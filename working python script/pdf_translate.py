#!/usr/bin/env python3
"""
pdf_translate.py - translate a PDF into one or more languages while keeping the layout.

Handles
  * vector text (normal PDF text objects)
  * text baked into raster images (OCR via Google Cloud Vision, or Tesseract offline)
  * mixed pages (vector text drawn on top of / next to raster images)

Layout rules
  * original text is removed WITHOUT painting a white box (redaction with no fill,
    raster text is inpainted from the surrounding pixels)
  * translated text first grows to the right / downward until it hits a border
    (drawn lines such as dotted frames, other text, image or page edge) and wraps there
  * the font is shrunk only if the wrapped text still does not fit (down to --min-scale)

Usage
  python pdf_translate.py input.pdf --langs de es fr \
      --translate-key $GOOGLE_TRANSLATE_KEY --vision-key $GOOGLE_VISION_KEY \
      --keep "DIY Plans" --keep "WOODSHOPKIDS.ETSY.COM"

  Offline dry run (fake "longer" translation + Tesseract OCR, no API calls):
  python pdf_translate.py input.pdf --langs de --translator pseudo --ocr tesseract --debug

Requirements
  pip install pymupdf>=1.24.10 opencv-python-headless numpy requests
  optional: pip install deep-translator   (for --translator free)
  optional: pip install pytesseract + tesseract binary (for --ocr tesseract)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

import cv2
import numpy as np
import pymupdf
import requests

log = logging.getLogger("pdf_translate")

# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------
BULLET_RE = re.compile(r"^([●•▪■◦·‣∙\-–—*]\s+)")
URL_RE = re.compile(
    r"(?:https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|"
    r"\b(?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|de|fr|es|eu|co|uk|lv|info|biz|app)\b)",
    re.I,
)
UNIT_WORDS = {
    "mm", "cm", "dm", "m", "km", "m²", "m2", "m³", "m3", "kg", "g", "t", "pc", "pcs",
    "ft", "in", "ft²", "eur", "usd", "gbp", "kw", "w", "v", "l", "ml",
}
SENTENCE_END = tuple(".!?:;")


def rgb_from_int(c: int) -> tuple:
    return (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)


def hex_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v * 255)))) for v in rgb)


def color_dist(a, b) -> float:
    return max(abs(x - y) for x, y in zip(a, b))


def family_from_font(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ("courier", "mono", "consol")):
        return "monospace"
    if any(k in n for k in ("times", "serif", "bodoni", "georgia", "garamond", "roman", "cambria")) \
            and "sans" not in n:
        return "serif"
    return "sans-serif"


def needs_translation(text: str, keep_terms) -> bool:
    """False for pure numbers / dimensions / units / protected terms."""
    s = URL_RE.sub(" ", text)
    for k in keep_terms:
        s = s.replace(k, " ")
    words = re.findall(r"[^\W\d_]{2,}", s)
    return any(w.lower() not in UNIT_WORDS for w in words)


def overlap_frac(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """Intersection area / area of the smaller rect."""
    x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
    x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    small = min(a.width * a.height, b.width * b.height) or 1e-6
    return inter / small


def y_overlap(a, b, tol=1.0) -> bool:
    return a.y0 < b.y1 - tol and a.y1 > b.y0 + tol


def x_overlap(ax0, ax1, bx0, bx1, tol=1.0) -> bool:
    return ax0 < bx1 - tol and ax1 > bx0 + tol


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------
@dataclass(eq=False)
class Line:
    text: str
    rect: pymupdf.Rect
    size: float
    color: tuple
    source: str                      # "vector" | "ocr"
    bold: bool = False
    italic: bool = False
    family: str = "sans-serif"
    dir: tuple = (1.0, 0.0)
    group: object = None             # OCR engine paragraph id (lines of different groups never merge)
    hyphen: bool = False
    ocr_ref: tuple | None = None     # (xref, line_index) for raster lines
    min_conf: float = 1.0            # lowest per-character (Vision) / per-word (Tesseract) confidence
    px_height: float = 0.0           # text height in native image pixels (resolution check)


@dataclass(eq=False)
class Unit:
    """One box of text that is laid out as a whole."""
    paras: list                      # list[list[Line]]
    bounds: pymupdf.Rect             # hard outer limit (page or image area)
    source: str
    verbatim: bool = False           # raster text re-drawn as vector without translating (numbers)
    align_hint: str = "left"

    @property
    def lines(self):
        return [ln for p in self.paras for ln in p]

    @property
    def rect(self):
        r = pymupdf.Rect(self.lines[0].rect)
        for ln in self.lines[1:]:
            r |= ln.rect
        return r

    def _major(self, attr):
        vals = [getattr(ln, attr) for ln in self.lines]
        return max(set(vals), key=vals.count)

    @property
    def size(self):
        return median(ln.size for ln in self.lines)

    @property
    def rotate(self):
        dx, dy = self.lines[0].dir
        if abs(dx - 1) < 1e-3:
            return 0
        if abs(dy + 1) < 1e-3:
            return 90
        if abs(dx + 1) < 1e-3:
            return 180
        if abs(dy - 1) < 1e-3:
            return 270
        return None                   # arbitrary angle - not supported

    @property
    def line_height(self):
        ls = self.lines
        if len(ls) < 2:
            return 1.2
        pitches = [b.rect.y0 - a.rect.y0 for a, b in zip(ls, ls[1:]) if b.rect.y0 > a.rect.y0]
        if not pitches:
            return 1.2
        return max(1.0, min(1.6, median(pitches) / self.size))


def para_text(lines) -> str:
    out = ""
    for ln in lines:
        t = ln.text.strip()
        if not out:
            out = t
        elif re.search(r"[^\W\d_]-$", out) and t[:1].islower():
            out = out[:-1] + t                                  # de-hyphenate
        else:
            out += " " + t
    return re.sub(r"\s+", " ", out)


def split_prefix(text: str):
    m = BULLET_RE.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


# --------------------------------------------------------------------------------------
# Vector text + drawings
# --------------------------------------------------------------------------------------
def extract_vector_lines(page) -> list:
    invisible = []
    try:
        for t in page.get_texttrace():
            if t.get("type") == 3 or t.get("opacity", 1) == 0:
                invisible.append(pymupdf.Rect(t["bbox"]))
    except Exception:  # noqa: BLE001 - old PyMuPDF
        pass

    lines = []
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            spans = []
            for s in l["spans"]:
                if s.get("alpha", 255) == 0:
                    continue
                r = pymupdf.Rect(s["bbox"])
                if any(overlap_frac(r, inv) > 0.8 for inv in invisible):
                    continue
                spans.append(s)
            text = re.sub(r"\s+", " ", "".join(s["text"] for s in spans)).strip()
            if not text:
                continue
            main = max((s for s in spans if s["text"].strip()), key=lambda s: len(s["text"].strip()))
            rect = pymupdf.Rect(spans[0]["bbox"])
            for s in spans[1:]:
                if s["text"].strip():
                    rect |= pymupdf.Rect(s["bbox"])
            font = main["font"]
            lines.append(Line(
                text=text, rect=rect, size=main["size"], color=rgb_from_int(main["color"]),
                source="vector",
                bold=bool(main["flags"] & 16) or any(k in font.lower() for k in ("bold", "black", "heavy")),
                italic=bool(main["flags"] & 2) or any(k in font.lower() for k in ("italic", "oblique")),
                family=family_from_font(font), dir=tuple(l["dir"]),
            ))
    return lines


def extract_segments(page, min_len=25.0, gap=5.0):
    """Horizontal / vertical drawn lines (merged, so dashed borders become one segment)."""
    vb, hb = defaultdict(list), defaultdict(list)

    def add(p1, p2):
        if abs(p1.x - p2.x) < 0.8:
            vb[round((p1.x + p2.x) / 2)].append((min(p1.y, p2.y), max(p1.y, p2.y)))
        elif abs(p1.y - p2.y) < 0.8:
            hb[round((p1.y + p2.y) / 2)].append((min(p1.x, p2.x), max(p1.x, p2.x)))

    for p in page.get_drawings():
        if p.get("color") is None and p.get("fill") is None:
            continue
        for it in p["items"]:
            op = it[0]
            if op == "l":
                add(it[1], it[2])
            elif op == "re":
                r = it[1]
                add(r.tl, r.tr); add(r.bl, r.br); add(r.tl, r.bl); add(r.tr, r.br)
            elif op == "qu":
                q = it[1]
                add(q.ul, q.ur); add(q.ll, q.lr); add(q.ul, q.ll); add(q.ur, q.lr)

    def merge(buckets):
        out = []
        for k, ivs in buckets.items():
            ivs.sort()
            s, e = ivs[0]
            for a, b in ivs[1:]:
                if a <= e + gap:
                    e = max(e, b)
                else:
                    if e - s >= min_len:
                        out.append((float(k), s, e))
                    s, e = a, b
            if e - s >= min_len:
                out.append((float(k), s, e))
        return out

    return merge(vb), merge(hb)


# --------------------------------------------------------------------------------------
# Grouping lines into paragraphs / boxes
# --------------------------------------------------------------------------------------
def cluster_lines(lines):
    horiz = [l for l in lines if abs(l.dir[0] - 1) < 1e-3 and abs(l.dir[1]) < 1e-3]
    other = [[l] for l in lines if l not in horiz]
    horiz.sort(key=lambda l: (l.rect.y0, l.rect.x0))
    clusters = []
    for ln in horiz:
        target = None
        for c in reversed(clusters):
            last = c[-1]
            if last.group != ln.group or last.source != ln.source:
                continue
            s = max(last.size, ln.size)
            if abs(last.size - ln.size) > 0.2 * s:
                continue
            if ln.source == "vector" and last.bold != ln.bold:
                continue
            if color_dist(last.color, ln.color) > 0.3:
                continue
            gap = ln.rect.y0 - last.rect.y1
            if gap < -0.3 * s or gap > 0.8 * s:
                continue
            left_ok = abs(ln.rect.x0 - last.rect.x0) <= 0.8 * s
            center_ok = abs((ln.rect.x0 + ln.rect.x1) - (last.rect.x0 + last.rect.x1)) / 2 <= 0.8 * s
            if not (left_ok or center_ok):
                continue
            target = c
            break
        if target is not None:
            target.append(ln)
        else:
            clusters.append([ln])
    return clusters + other


def split_paragraphs(cluster):
    paras = [[cluster[0]]]
    for prev, cur in zip(cluster, cluster[1:]):
        p, c = prev.text.rstrip(), cur.text.lstrip()
        soft = (not p.endswith(SENTENCE_END) and c[:1].islower()) or p.endswith((",", "-")) \
            or prev.hyphen
        if soft and not BULLET_RE.match(c):
            paras[-1].append(cur)
        else:
            paras.append([cur])
    return paras


def right_limit(rect, bounds, vsegs, obstacles, pad):
    right = bounds.x1
    for x, y0, y1 in vsegs:
        if x >= rect.x1 - 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            right = min(right, x - pad)
    for o in obstacles:
        if o.x0 >= rect.x1 - 1 and y_overlap(o, rect):
            right = min(right, o.x0 - pad)
    return max(right, rect.x1)


def left_limit(rect, bounds, vsegs, obstacles, pad):
    left = bounds.x0
    for x, y0, y1 in vsegs:
        if x <= rect.x0 + 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            left = max(left, x + pad)
    for o in obstacles:
        if o.x1 <= rect.x0 + 1 and y_overlap(o, rect):
            left = max(left, o.x1 + pad)
    return min(left, rect.x0)


def vectorizable(line, vec_cfg) -> bool:
    """Raster text we are sure enough about to redraw as vector text (e.g. numbers)."""
    if vec_cfg is None or line.source != "ocr":
        return False
    if not re.search(r"[0-9A-Za-z\u00C0-\u024F]", line.text):
        return False                      # lone dashes / symbols: keep the pixels
    return line.min_conf >= vec_cfg["min_conf"] and line.px_height >= vec_cfg["min_px"]


def column_alignment(unit_lines, all_lines):
    """left / right / center, judged from other raster lines stacked in the same column."""
    R = pymupdf.Rect(unit_lines[0].rect)
    for ln in unit_lines[1:]:
        R |= ln.rect
    size = median(l.size for l in unit_lines)
    own = set(map(id, unit_lines))
    votes = {"left": 0, "right": 0, "center": 0}
    for o in all_lines:
        if id(o) in own or o.source != "ocr" or o.group is None or unit_lines[0].group is None \
                or o.group[0] != unit_lines[0].group[0] or y_overlap(o.rect, R):
            continue
        if min(abs(o.rect.y0 - R.y1), abs(R.y0 - o.rect.y1)) > 12 * size:
            continue
        tol = 0.4 * size
        l_ok = abs(o.rect.x0 - R.x0) < tol
        r_ok = abs(o.rect.x1 - R.x1) < tol
        c_ok = abs((o.rect.x0 + o.rect.x1) - (R.x0 + R.x1)) / 2 < tol
        if r_ok and not l_ok:
            votes["right"] += 1
        elif l_ok and not r_ok:
            votes["left"] += 1
        elif c_ok and not (l_ok or r_ok):
            votes["center"] += 1
    best = max(votes, key=votes.get)
    if votes[best] == 0:
        numeric = not re.search(r"[^\W\d_]", para_text(unit_lines))
        return "right" if numeric else "left"
    return best


def build_units(lines, bounds_for, vsegs, keep_terms, vec_cfg=None):
    """Clusters -> Units. Free-standing blocks are reflowed as one box ("flow"),
    blocks with row-aligned neighbours (tables) are laid out per paragraph.
    Raster text that needs no translation (numbers, dimensions) becomes a `verbatim` unit
    when the OCR is confident, so it is also crisp vector text; otherwise its pixels stay."""
    units = []
    all_rects = [(ln, ln.rect) for ln in lines]

    def add_verbatim(para, bounds):
        if all(vectorizable(ln, vec_cfg) for ln in para):
            units.append(Unit(paras=[para], bounds=bounds, source="ocr", verbatim=True,
                              align_hint=column_alignment(para, lines)))

    for cluster in cluster_lines(lines):
        paras = split_paragraphs(cluster)
        if not any(needs_translation(para_text(p), keep_terms) for p in paras):
            if cluster[0].source == "ocr":
                for p in paras:
                    add_verbatim(p, bounds_for(p[0]))
            continue
        bounds = bounds_for(cluster[0])
        own = set(map(id, cluster))
        others = [r for ln, r in all_rects if id(ln) not in own]
        crect = pymupdf.Rect(cluster[0].rect)
        for ln in cluster[1:]:
            crect |= ln.rect
        flow = len(paras) > 1
        if flow:
            # table-like = at least two lines of the block have their own row-aligned
            # neighbour text (left or right). One stray label beside the block is fine.
            seg_right = right_limit(crect, bounds, vsegs, [], 2)
            seg_left = left_limit(crect, bounds, vsegs, [], 2)
            rows_with_neighbours = 0
            for ln in cluster:
                lr = ln.rect
                for r in others:
                    if r.height > 1.6 * lr.height or not y_overlap(r, lr):
                        continue
                    if (r.x0 >= crect.x1 - 1 and r.x0 < seg_right) or \
                            (r.x1 <= crect.x0 + 1 and r.x1 > seg_left):
                        rows_with_neighbours += 1
                        break
            flow = rows_with_neighbours < 2
        if flow or len(paras) == 1:
            units.append(Unit(paras=paras, bounds=bounds, source=cluster[0].source))
        else:
            for p in paras:
                if needs_translation(para_text(p), keep_terms):
                    units.append(Unit(paras=[p], bounds=bounds, source=p[0].source))
                elif p[0].source == "ocr":
                    add_verbatim(p, bounds)
    return units


# --------------------------------------------------------------------------------------
# OCR engines
# --------------------------------------------------------------------------------------
@dataclass
class OcrLine:
    text: str
    box: tuple          # normalised (x0, y0, x1, y1), 0..1 in image space
    words: list         # normalised word boxes
    conf: float
    group: object
    hyphen: bool = False
    color: tuple = (0, 0, 0)
    ink: float = 0.0
    bold: bool = False
    height: float = 0.0         # normalised height used for the font size
    min_conf: float = 1.0
    px_height: float = 0.0      # box height in native image pixels


def _split_on_gaps(words, group, conf, hyphen):
    """words: [(text, (x0,y0,x1,y1)[, sep_after])] in px. Split where the horizontal gap is big
    (table columns)."""
    if not words:
        return []
    words = [w if len(w) == 4 else (w[0], w[1], w[2] if len(w) > 2 else " ", conf) for w in words]
    h = median(w[1][3] - w[1][1] for w in words)
    segs, cur = [], [words[0]]
    for w in words[1:]:
        if w[1][0] - cur[-1][1][2] > 1.2 * h:
            segs.append(cur)
            cur = [w]
        else:
            cur.append(w)
    segs.append(cur)
    out = []
    for i, seg in enumerate(segs):
        boxes = [b for _, b, _, _ in seg]
        out.append(dict(
            min_conf=min(c for _, _, _, c in seg),
            text="".join(t + sep for t, _, sep, _ in seg).strip(),
            box=(min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)),
            words=boxes, conf=conf, group=group, hyphen=hyphen and i == len(segs) - 1,
        ))
    return out


class VisionOCR:
    URL = "https://vision.googleapis.com/v1/images:annotate"

    def __init__(self, key, lang_hint=None):
        self.key, self.lang_hint = key, lang_hint

    def detect(self, img_bgr):
        ok, buf = cv2.imencode(".png", img_bgr)
        if len(buf) > 7_000_000:
            ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        req = {"image": {"content": base64.b64encode(buf.tobytes()).decode()},
               "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]}
        if self.lang_hint:
            req["imageContext"] = {"languageHints": [self.lang_hint]}
        for attempt in range(4):
            r = requests.post(self.URL, params={"key": self.key}, json={"requests": [req]}, timeout=120)
            if r.status_code in (429, 500, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            break
        resp = r.json()["responses"][0]
        if "error" in resp:
            raise RuntimeError(f"Vision API error: {resp['error']}")
        lines = []
        for pi, pg in enumerate(resp.get("fullTextAnnotation", {}).get("pages", [])):
            for bi, blk in enumerate(pg.get("blocks", [])):
                for qi, para in enumerate(blk.get("paragraphs", [])):
                    cur, confs = [], []
                    for w in para.get("words", []):
                        syms = w.get("symbols", [])
                        t = "".join(s.get("text", "") for s in syms)
                        v = w.get("boundingBox", {}).get("vertices", [])
                        xs = [p["x"] for p in v if "x" in p] or [0]
                        ys = [p["y"] for p in v if "y" in p] or [0]
                        brk = (syms[-1].get("property", {}).get("detectedBreak", {}).get("type")
                               if syms else None)
                        sym_conf = min([s.get("confidence", w.get("confidence", 1.0)) for s in syms]
                                       or [w.get("confidence", 1.0)])
                        cur.append((t, (min(xs), min(ys), max(xs), max(ys)),
                                    " " if brk in ("SPACE", "SURE_SPACE") else "", sym_conf))
                        confs.append(w.get("confidence", 1.0))
                        if brk in ("EOL_SURE_SPACE", "LINE_BREAK", "HYPHEN"):
                            lines += _split_on_gaps(cur, (pi, bi, qi), sum(confs) / len(confs),
                                                    brk == "HYPHEN")
                            cur, confs = [], []
                    if cur:
                        lines += _split_on_gaps(cur, (pi, bi, qi), sum(confs) / len(confs), False)
        # Vision puts no space between words without a detected break (e.g. punctuation) - fine.
        return lines


class TesseractOCR:
    def __init__(self, lang="eng"):
        import pytesseract  # noqa: F401  (fail early if missing)
        self.lang = lang

    def detect(self, img_bgr):
        import pytesseract
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        d = pytesseract.image_to_data(rgb, lang=self.lang, output_type=pytesseract.Output.DICT)
        groups = defaultdict(list)
        for i, t in enumerate(d["text"]):
            if not t.strip() or float(d["conf"][i]) < 0:
                continue
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            groups[key].append((t, (x, y, x + w, y + h), float(d["conf"][i]) / 100))
        lines = []
        for key, ws in sorted(groups.items()):
            ws.sort(key=lambda w: w[1][0])
            conf = sum(w[2] for w in ws) / len(ws)
            lines += _split_on_gaps([(t, b, " ", c) for t, b, c in ws], key[:2], conf,
                                    ws[-1][0].endswith("-"))
        return lines


# --------------------------------------------------------------------------------------
# Raster images: OCR + inpainting
# --------------------------------------------------------------------------------------
@dataclass
class ImageData:
    xref: int
    img: np.ndarray            # BGR, native resolution
    alpha: np.ndarray | None
    ext: str
    lines: list                # list[OcrLine]
    vsegs: list = field(default_factory=list)   # normalised (u, v0, v1) straight lines in the image
    hsegs: list = field(default_factory=list)   # normalised (v, u0, u1)


def load_image(doc, xref):
    info = doc.extract_image(xref)
    pix = pymupdf.Pixmap(doc, xref)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    alpha = None
    smask = info.get("smask", 0)
    if smask:
        try:
            sp = pymupdf.Pixmap(doc, smask)
            if sp.n != 1:
                sp = pymupdf.Pixmap(pymupdf.csGRAY, sp)
            a = np.frombuffer(sp.samples, np.uint8).reshape(sp.h, sp.w)
            alpha = cv2.resize(a, (pix.w, pix.h)) if a.shape != (pix.h, pix.w) else a.copy()
        except Exception as e:  # noqa: BLE001
            log.warning("could not read soft mask of image %s: %s", xref, e)
    return img, alpha, info.get("ext", "png")


def raster_segments(img, min_frac=0.08):
    """Long straight (also dashed) lines inside a raster image, e.g. table/box borders."""
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    out_v, out_h = [], []
    for vertical in (False, True):
        L = max(25, int((H if vertical else W) * min_frac))
        close_k = (1, 5) if vertical else (5, 1)             # join dashes / dots
        open_k = (1, L) if vertical else (L, 1)
        m = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, close_k))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, open_k))
        n, _, stats, _ = cv2.connectedComponentsWithStats(m)
        for x, y, w, h, _a in stats[1:]:
            if vertical and h >= L and w <= 8:
                out_v.append(((x + w / 2) / W, y / H, (y + h) / H))
            elif not vertical and w >= L and h <= 8:
                out_h.append(((y + h / 2) / H, x / W, (x + w) / W))
    return out_v, out_h


def ink_mask(region):
    """Pixels that differ clearly from the local background colour (estimated on the border)."""
    if region.size == 0:
        return np.zeros(region.shape[:2], bool), np.zeros(3)
    ring = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]]).astype(float)
    bg = np.median(ring, axis=0)
    dist = np.linalg.norm(region.astype(float) - bg, axis=2)
    m = dist > 30
    if m.mean() > 0.65:            # busy/textured background -> just take the whole box
        m[:] = True
    return m, bg


def analyze_ocr_lines(img, raw_lines, scale):
    """raw px lines (on the scaled OCR image) -> OcrLine with colour / ink ratio from native image."""
    H, W = img.shape[:2]
    out = []
    for rl in raw_lines:
        x0, y0, x1, y1 = [v / scale for v in rl["box"]]
        box = (x0 / W, y0 / H, x1 / W, y1 / H)
        words = [tuple(v / scale / (W if i % 2 == 0 else H) for i, v in enumerate(b)) for b in rl["words"]]
        xi0, yi0, xi1, yi1 = int(max(0, x0 - 2)), int(max(0, y0 - 2)), int(min(W, x1 + 2)), int(min(H, y1 + 2))
        reg = img[yi0:yi1, xi0:xi1]
        color, ink = (0, 0, 0), 0.0
        if reg.size:
            m, bg = ink_mask(reg)
            ink = float(m.mean())
            if m.any():
                px = reg[m].astype(float)
                d = np.linalg.norm(px - bg, axis=1)
                sel = px[d >= np.quantile(d, 0.6)]
                b, g, r = sel.mean(axis=0) / 255
                color = (r, g, b)
        out.append(OcrLine(text=rl["text"], box=box, words=words, conf=rl["conf"], group=rl["group"],
                           min_conf=rl.get("min_conf", rl["conf"]), px_height=y1 - y0,
                           hyphen=rl["hyphen"], color=color, ink=ink))
    if out:
        med = median(l.ink for l in out)
        # OCR boxes hug the ink: estimate the em size from the box height depending on
        # whether the line has descenders (g, p, y, brackets ...) or not.
        ems = []
        for l in out:
            h = l.box[3] - l.box[1]
            has_desc = bool(re.search(r"[gjpqyQ(),;\[\]{}|/]", l.text))
            ems.append(h / (0.92 if has_desc else 0.72))
        em_med = median(ems)
        for l, em in zip(out, ems):
            numeric = not re.search(r"[^\W\d_]", l.text)
            l.bold = med > 0 and l.ink > (1.4 if numeric else 1.2) * med   # digits vary more
            # snap body text to one common size so a table does not get mixed sizes
            l.height = em_med if 0.75 * em_med <= em <= 1.35 * em_med else em
    return out


def ocr_image(doc, xref, engine, cache_dir):
    img, alpha, ext = load_image(doc, xref)
    H, W = img.shape[:2]
    longest = max(H, W)
    scale = 1.0
    if longest < 1800:
        scale = min(3.0, 1800 / longest)
    elif longest > 4000:
        scale = 4000 / longest
    key = hashlib.sha1(img.tobytes()).hexdigest() + f"_{type(engine).__name__}_v2"
    cpath = os.path.join(cache_dir, "ocr", key + ".json") if cache_dir else None
    if cpath and os.path.exists(cpath):
        with open(cpath, encoding="utf8") as f:
            raw = json.load(f)
    else:
        work = img if scale == 1.0 else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        raw = engine.detect(work)
        if cpath:
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            with open(cpath, "w", encoding="utf8") as f:
                json.dump(raw, f, default=list)
    for r in raw:
        r["group"] = tuple(r["group"]) if isinstance(r["group"], list) else r["group"]
    lines = analyze_ocr_lines(img, raw, scale)
    vs, hs = raster_segments(img)

    def crosses_text(horizontal, c, a0, a1):
        for l in lines:                       # segment running through the core of a text line
            x0, y0, x1, y1 = l.box
            w, h = x1 - x0, y1 - y0
            if horizontal and y0 + 0.2 * h < c < y1 - 0.2 * h and a0 < x1 and a1 > x0:
                return True
            if not horizontal and x0 + 0.05 * w < c < x1 - 0.05 * w and a0 < y1 - 0.2 * h and a1 > y0 + 0.2 * h:
                return True
        return False
    vs = [sg for sg in vs if not crosses_text(False, *sg)]
    hs = [sg for sg in hs if not crosses_text(True, *sg)]
    return ImageData(xref=xref, img=img, alpha=alpha, ext=ext, lines=lines,
                     vsegs=vs, hsegs=hs)


def erase_text(data: ImageData, line_idxs):
    img = data.img.copy()
    H, W = img.shape[:2]
    mask = np.zeros((H, W), np.uint8)
    for i in line_idxs:
        ln = data.lines[i]
        for (u0, v0, u1, v1) in ln.words:
            x0, y0, x1, y1 = u0 * W, v0 * H, u1 * W, v1 * H
            pad = max(2.0, 0.15 * (y1 - y0))
            xi0, yi0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
            xi1, yi1 = int(min(W, x1 + pad)), int(min(H, y1 + pad))
            if xi1 <= xi0 or yi1 <= yi0:
                continue
            m, _ = ink_mask(img[yi0:yi1, xi0:xi1])
            mask[yi0:yi1, xi0:xi1][m] = 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=3)
    out = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
    if data.alpha is not None:
        rgba = np.dstack([out, data.alpha])
        ok, buf = cv2.imencode(".png", rgba)
    elif data.ext.lower() in ("jpg", "jpeg", "jpx"):
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
    else:
        ok, buf = cv2.imencode(".png", out)
    return buf.tobytes()


# --------------------------------------------------------------------------------------
# Translators
# --------------------------------------------------------------------------------------
class BaseTranslator:
    def __init__(self, target, source=None, keep_terms=(), cache=None):
        self.target, self.source = target, source
        self.keep = sorted(set(keep_terms), key=len, reverse=True)
        self.cache = cache if cache is not None else {}

    def translate_many(self, texts):
        todo = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if todo:
            log.info("  translating %d new segments (%d chars) -> %s",
                     len(todo), sum(map(len, todo)), self.target)
            for chunk in self._chunks(todo):
                for src, dst in zip(chunk, self._translate(chunk)):
                    self.cache[src] = dst
        return {t: self.cache[t] for t in texts}

    @staticmethod
    def _chunks(items, max_items=40, max_chars=6000):
        chunk, n = [], 0
        for t in items:
            if chunk and (len(chunk) >= max_items or n + len(t) > max_chars):
                yield chunk
                chunk, n = [], 0
            chunk.append(t)
            n += len(t)
        if chunk:
            yield chunk

    def _protected_spans(self, text):
        spans = [m.span() for m in URL_RE.finditer(text)]
        for k in self.keep:
            for m in re.finditer(re.escape(k), text):
                spans.append(m.span())
        spans.sort()
        merged = []
        for s, e in spans:
            if merged and s < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
            else:
                merged.append((s, e))
        return merged


class CloudTranslator(BaseTranslator):
    """Google Cloud Translation v2 (API key). 500k chars/month free tier."""
    URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, key, **kw):
        super().__init__(**kw)
        self.key = key

    def _to_html(self, text):
        out, pos = [], 0
        for s, e in self._protected_spans(text):
            out.append(html.escape(text[pos:s]))
            out.append('<span translate="no">' + html.escape(text[s:e]) + "</span>")
            pos = e
        out.append(html.escape(text[pos:]))
        return "".join(out)

    def _translate(self, chunk):
        body = {"q": [self._to_html(t) for t in chunk], "target": self.target, "format": "html"}
        if self.source:
            body["source"] = self.source
        for attempt in range(5):
            r = requests.post(self.URL, params={"key": self.key}, json=body, timeout=120)
            if r.status_code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            break
        res = []
        for tr in r.json()["data"]["translations"]:
            t = re.sub(r"</?span[^>]*>", "", tr["translatedText"])
            res.append(re.sub(r"\s+", " ", html.unescape(t)).strip())
        return res


class GeminiTranslator(BaseTranslator):
    @staticmethod
    def _chunks(items, max_items=40, max_chars=6000):
        return BaseTranslator._chunks(items, max_items, max_chars)

    """Translate via the Gemini API (generativelanguage.googleapis.com), not Cloud Translation."""

    def __init__(self, key, model="gemini-2.5-flash", **kw):
        super().__init__(**kw)
        self.key, self.model = key, model
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def _mask(self, text):
        spans = self._protected_spans(text)
        out, saved, pos = "", [], 0
        for s, e in spans:
            out += text[pos:s] + f"\u2060{len(saved)}\u2060"
            saved.append(text[s:e])
            pos = e
        return out + text[pos:], saved

    def _unmask(self, text, saved):
        def sub(m):
            i = int(m.group(1))
            return saved[i] if 0 <= i < len(saved) else m.group(0)
        return re.sub(r"\u2060(\d+)\u2060", sub, text)

    def _translate(self, chunk):
        masked, saved_all = [], []
        for t in chunk:
            m, saved = self._mask(t)
            masked.append(m)
            saved_all.append(saved)
        src = f" from {self.source}" if self.source else ""
        prompt = (
            f"Translate each string in this JSON array{src} into {self.target}. "
            "Keep any \u2060N\u2060 markers exactly as-is (do not translate or remove them, "
            "they are placeholders). This is text from architectural / DIY construction plans, "
            "so keep it concise and use standard technical/construction terminology. "
            "Return ONLY a JSON array of the same length, same order, translated strings, "
            "no markdown, no commentary.\n\n" + json.dumps(masked, ensure_ascii=False)
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        for attempt in range(5):
            r = requests.post(self.url, params={"key": self.key}, json=body, timeout=180)
            if r.status_code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:500]}")
            break
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:500]}") from e
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            out = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Gemini did not return valid JSON: {text[:500]}") from e
        if len(out) != len(chunk):
            raise RuntimeError(f"Gemini returned {len(out)} items, expected {len(chunk)}")
        return [re.sub(r"\s+", " ", self._unmask(t, saved)).strip()
                for t, saved in zip(out, saved_all)]


class FreeTranslator(BaseTranslator):
    """Unofficial free Google Translate endpoint via deep-translator (rate limited, no SLA)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        from deep_translator import GoogleTranslator
        self.gt = GoogleTranslator(source=self.source or "auto", target=self.target)

    def _translate(self, chunk):
        res = []
        for t in chunk:
            spans = self._protected_spans(t)
            masked, saved, pos = "", [], 0
            for i, (s, e) in enumerate(spans):
                masked += t[pos:s] + f"[{i}]"
                saved.append(t[s:e])
                pos = e
            masked += t[pos:]
            for attempt in range(4):
                try:
                    out = self.gt.translate(masked) or t
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            for i, v in enumerate(saved):
                out = re.sub(rf"\[\s*{i}\s*\]", lambda _m, v=v: v, out)
            res.append(out)
            time.sleep(0.15)
        return res


class PseudoTranslator(BaseTranslator):
    """Offline test mode: makes text ~40% longer with accents, to test wrapping/shrinking."""
    ACC = str.maketrans("aeiouAEIOUcns", "äéîöüÄÉÎÖÜçñß")

    def _translate(self, chunk):
        res = []
        for t in chunk:
            spans = self._protected_spans(t)
            out, pos = "", 0

            def fake(s):
                return re.sub(r"[^\W\d_]{4,}", lambda m: (m.group() + m.group()[: len(m.group()) // 2]).translate(self.ACC), s)
            for s, e in spans:
                out += fake(t[pos:s]) + t[s:e]
                pos = e
            res.append(out + fake(t[pos:]))
        return res


# --------------------------------------------------------------------------------------
# Analysis (language independent) and rendering (per language)
# --------------------------------------------------------------------------------------
@dataclass
class PageData:
    units: list
    obstacle_lines: list        # every text line on the page (kept or translated)
    vsegs: list
    hsegs: list


def analyze(doc, ocr_engine, args):
    keep = args.keep
    pages = []
    image_cache = {}
    erase = defaultdict(set)          # xref -> line indexes to inpaint
    xref_page = {}
    for pno, page in enumerate(doc):
        if page.rotation:
            page.remove_rotation()
        vlines = extract_vector_lines(page)
        vsegs, hsegs = extract_segments(page)
        page_bounds = page.rect + (args.margin, args.margin, -args.margin, -args.margin)
        ocr_lines, line_bounds = [], {}

        if ocr_engine is not None:
            infos = page.get_image_info(xrefs=True)
            visible_vec = [l.rect for l in vlines]
            for idx, info in enumerate(infos):
                xref = info.get("xref", 0)
                bbox = pymupdf.Rect(info["bbox"])
                if xref <= 0 or bbox.width < args.min_image_pt or bbox.height < args.min_image_pt / 3:
                    continue
                M = pymupdf.Matrix(info["transform"])
                if abs(M.b) > 1e-3 or abs(M.c) > 1e-3 or M.a <= 0 or M.d <= 0:
                    log.warning("p%d: image %d is rotated/mirrored - OCR skipped", pno + 1, xref)
                    continue
                if xref not in image_cache:
                    try:
                        image_cache[xref] = ocr_image(doc, xref, ocr_engine, args.cache_dir)
                        log.info("p%d: OCR image xref %d -> %d lines", pno + 1, xref,
                                 len(image_cache[xref].lines))
                    except Exception as e:  # noqa: BLE001
                        log.error("p%d: OCR failed for image %d: %s", pno + 1, xref, e)
                        image_cache[xref] = None
                data = image_cache[xref]
                if data is None:
                    continue
                later = [pymupdf.Rect(i2["bbox"]) for i2 in infos[idx + 1:]]
                area = bbox & page.rect
                if data.lines:
                    for u, v0, v1 in data.vsegs:
                        p0, p1 = pymupdf.Point(u, v0) * M, pymupdf.Point(u, v1) * M
                        vsegs.append((p0.x, p0.y, p1.y))
                    for v, u0, u1 in data.hsegs:
                        p0, p1 = pymupdf.Point(u0, v) * M, pymupdf.Point(u1, v) * M
                        hsegs.append((p0.y, p0.x, p1.x))
                for li, ol in enumerate(data.lines):
                    r = pymupdf.Rect(ol.box[0], ol.box[1], ol.box[2], ol.box[3]) * M
                    if r.height * 1.0 < args.min_text_pt or ol.conf < args.min_conf:
                        continue
                    if overlap_frac(r, page.rect) < 0.95 or not r.intersects(area):
                        continue
                    if any(overlap_frac(r, v) > 0.3 for v in visible_vec):
                        continue                          # already vector text
                    c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                    if any(c in l2 for l2 in later):
                        continue                          # hidden under another image
                    size = (ol.height or (ol.box[3] - ol.box[1])) * M.d * args.ocr_size_factor
                    ln = Line(text=ol.text, rect=r, size=size,
                              color=ol.color, source="ocr", bold=ol.bold, family=args.ocr_font,
                              group=(xref, ol.group), hyphen=ol.hyphen, ocr_ref=(xref, li),
                              min_conf=ol.min_conf, px_height=ol.px_height)
                    ocr_lines.append(ln)
                    line_bounds[id(ln)] = area + (1, 1, -1, -1)

        all_lines = vlines + ocr_lines
        vec_cfg = None if args.keep_raster_numbers else {"min_conf": args.vector_min_conf,
                                                         "min_px": args.vector_min_px}
        units = build_units(all_lines, lambda ln: line_bounds.get(id(ln), page_bounds), vsegs, keep,
                            vec_cfg)
        for u in units:
            for ln in u.lines:
                if ln.ocr_ref:
                    erase[ln.ocr_ref[0]].add(ln.ocr_ref[1])
                    xref_page.setdefault(ln.ocr_ref[0], pno)
        pages.append(PageData(units=units, obstacle_lines=all_lines, vsegs=vsegs, hsegs=hsegs))
        log.info("p%d: %d vector lines, %d OCR lines, %d text boxes to translate",
                 pno + 1, len(vlines), len(ocr_lines), len(units))

    replaced = {}
    for xref, idxs in erase.items():
        replaced[xref] = (xref_page[xref], erase_text(image_cache[xref], idxs))
    return pages, replaced


def unit_segments(unit):
    """[(prefix, text_to_translate_or_None, original)] per paragraph."""
    out = []
    for p in unit.paras:
        t = para_text(p)
        prefix, body = split_prefix(t)
        out.append((prefix, body))
    return out


def layout_verbatim(unit, pd: PageData, placed, args):
    """Box for raster text redrawn unchanged: same place, same alignment as the original
    (right-aligned numbers stay right-aligned), a little slack in case our font is wider."""
    R = unit.rect
    size = unit.size
    pad = max(0.5, 0.15 * size)
    own = set(map(id, unit.lines))
    obstacles = [ln.rect for ln in pd.obstacle_lines if id(ln) not in own]
    right = right_limit(R, unit.bounds, pd.vsegs, obstacles, pad)
    left = left_limit(R, unit.bounds, pd.vsegs, obstacles, pad)
    slack = 0.35 * R.width + size
    align = unit.align_hint
    if align == "right":
        x0, x1 = max(left, R.x0 - slack), R.x1 + 0.5
    elif align == "center":
        half = R.width / 2 + min(slack / 2, R.x0 - left, right - R.x1)
        cx = (R.x0 + R.x1) / 2
        x0, x1 = cx - half, cx + half
    else:
        x0, x1 = R.x0 - 0.5, min(right, R.x1 + slack)
    lh = max(unit.line_height, 1.2)
    top = R.y0 - (lh - 1) / 2 * size
    bottom = R.y0 + len(unit.lines) * size * lh * 1.02 + 0.5
    return pymupdf.Rect(x0, top, x1, max(bottom, R.y1)), align


def layout_rect(unit, pd: PageData, placed, args, probe=None):
    R = unit.rect
    if unit.rotate != 0:
        return R, "left"
    if unit.verbatim:
        return layout_verbatim(unit, pd, placed, args)
    size = unit.size
    pad = max(1.0, 0.25 * size)
    own = set(map(id, unit.lines))
    obstacles = [ln.rect for ln in pd.obstacle_lines if id(ln) not in own] + placed
    b = unit.bounds
    right = right_limit(R, b, pd.vsegs, obstacles, pad)
    left = left_limit(R, b, pd.vsegs, obstacles, pad)
    bg = probe.background(R, size) if probe is not None else None
    if bg is not None:
        right = max(R.x1, min(right, probe.free_right(R, right + pad, bg) - pad))

    align, x0, x1 = "left", R.x0, right
    lines = unit.lines
    lgap, rgap = R.x0 - left, right - R.x1
    centers = [(l.rect.x0 + l.rect.x1) / 2 for l in lines]
    lefts = [l.rect.x0 for l in lines]
    centred_lines = len(lines) == 1 or (max(centers) - min(centers) < 0.5 * size
                                        and max(lefts) - min(lefts) > size)
    if centred_lines and lgap > 2 * size and abs(lgap - rgap) < max(3.0, 0.05 * (right - left)):
        cx = (R.x0 + R.x1) / 2
        half = min(cx - left, right - cx)
        align, x0, x1 = "center", cx - half, cx + half

    max_bottom = min(b.y1, R.y1 + args.max_grow * max(R.height, size * 1.3))
    hard = b.y1                                   # first real obstacle below (no padding)
    for y, sx0, sx1 in pd.hsegs:
        if y >= R.y1 - 0.5 and x_overlap(sx0, sx1, x0, x1):
            hard = min(hard, y)
    for o in obstacles:
        if o.y0 >= R.y1 - 1 and x_overlap(o.x0, o.x1, x0, x1):
            hard = min(hard, o.y0)
    if bg is not None:
        hard = min(hard, probe.free_down(x0, x1, R.y1, hard, bg))
    bottom = max(R.y1, min(max_bottom, hard - pad))
    # A PDF line bbox is tighter than size * line-height. Always allow room for the ORIGINAL
    # number of lines at 100% size: with the baseline aligned to the original (see `top` below)
    # the glyphs occupy the same place the original glyphs did, so only the invisible
    # line-box padding may touch the obstacle.
    need = R.y0 + len(lines) * size * max(unit.line_height, 1.2) * 1.02 + 0.5
    bottom = max(bottom, min(need, hard + 0.25 * size))
    # CSS line-height puts half of the extra leading above the first line; shift the box up by
    # that amount so the first baseline lands where the original one was
    top = R.y0 - (max(unit.line_height, 1.2) - 1) / 2 * size
    return pymupdf.Rect(x0, top, x1, bottom), align


class PixelProbe:
    """Low-res render of the page (1 px = 1 pt) used to see where the page is really empty.
    Only applied to text sitting on a clean background; over photos/renders the geometric
    limits alone are used (the original text overlapped the picture there anyway)."""

    def __init__(self, page, tol=40):
        pix = page.get_pixmap(dpi=72, alpha=False)
        self.a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3].astype(np.int16)
        self.sx, self.sy = pix.w / page.rect.width, pix.h / page.rect.height
        self.tol = tol

    def _box(self, x0, y0, x1, y1):
        H, W = self.a.shape[:2]
        return (int(max(0, min(W, x0 * self.sx))), int(max(0, min(H, y0 * self.sy))),
                int(max(0, min(W, x1 * self.sx))), int(max(0, min(H, y1 * self.sy))))

    def background(self, R, size):
        """Background colour around R, or None if the surroundings are busy."""
        ph, pv = min(0.5 * size, 8.0), min(0.3 * size, 5.0)
        X0, Y0, X1, Y1 = self._box(R.x0 - ph, R.y0 - pv, R.x1 + ph, R.y1 + pv)
        x0, y0, x1, y1 = self._box(R.x0, R.y0, R.x1, R.y1)
        reg = self.a[Y0:Y1, X0:X1]
        if reg.size == 0:
            return None
        m = np.ones(reg.shape[:2], bool)
        m[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
        ring = reg[m]
        if len(ring) < 20:
            return None
        bg = np.median(ring, axis=0)
        clean = (np.abs(ring - bg).max(axis=1) <= self.tol).mean()
        return bg if clean >= 0.85 else None

    def _ink(self, reg, bg):
        return np.abs(reg - bg).max(axis=2) > self.tol

    def free_right(self, R, limit, bg):
        x0, y0, x1, y1 = self._box(R.x1 + 1, R.y0 + 0.15 * R.height, limit, R.y1 - 0.15 * R.height)
        if x1 <= x0 or y1 <= y0:
            return limit
        cols = self._ink(self.a[y0:y1, x0:x1], bg).mean(axis=0)
        hit = np.nonzero(cols > 0.02)[0]
        return limit if len(hit) == 0 else (x0 + hit[0]) / self.sx

    def free_down(self, x0_, x1_, top, limit, bg):
        x0, y0, x1, y1 = self._box(x0_, top + 1, x1_, limit)
        if x1 <= x0 or y1 <= y0:
            return limit
        rows = self._ink(self.a[y0:y1, x0:x1], bg).mean(axis=1)
        hit = np.nonzero(rows > 0.01)[0]
        return limit if len(hit) == 0 else (y0 + hit[0]) / self.sy


def make_css(unit, align, font_css):
    fam = f"userfont, {unit.lines[0].family}" if font_css else unit.lines[0].family
    return (font_css + "* {margin:0; padding:0;} p {margin:0;} "
            f"body {{font-family: {fam}; font-size: {unit.size:.2f}px; line-height: {unit.line_height:.2f};"
            f" color: {hex_color(unit._major('color'))}; text-align: {align};"
            f" font-weight: {'bold' if unit._major('bold') else 'normal'};"
            f" font-style: {'italic' if unit._major('italic') else 'normal'};}}")


def render(src, pages, replaced, tmap, out_path, args, font_css, archive, debug=False):
    doc = pymupdf.open(src)
    stats = defaultdict(int)
    for page in doc:
        if page.rotation:
            page.remove_rotation()
    for xref, (pno, data) in replaced.items():
        try:
            doc[pno].replace_image(xref, stream=data)
        except Exception as e:  # noqa: BLE001
            log.error("could not replace image %s: %s", xref, e)

    for pno, (page, pd) in enumerate(zip(doc, pages)):
        if not pd.units:
            continue
        probe = PixelProbe(page)
        redacted = False
        for u in pd.units:
            for ln in u.lines:
                if ln.source == "vector":
                    r = ln.rect
                    inset = 0.2 * r.height
                    page.add_redact_annot(pymupdf.Rect(r.x0 + 0.3, r.y0 + inset, r.x1 - 0.3, r.y1 - inset),
                                          fill=False, cross_out=False)
                    redacted = True
        if redacted:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0),
                                  text=getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0))

        placed = []
        # redrawn raster numbers first: they keep their exact place and must not be squeezed
        # by translated neighbours that grew towards them
        for u in sorted(pd.units, key=lambda u: (not u.verbatim, round(u.rect.y0), u.rect.x0)):
            if u.rotate is None:
                log.warning("p%d: skipped text at unsupported angle: %r", pno + 1, para_text(u.lines))
                continue
            rect, align = layout_rect(u, pd, placed, args, probe)
            parts = []
            for prefix, body in unit_segments(u):
                txt = tmap.get(body, body)
                parts.append(html.escape(prefix + txt))
            body_html = "".join(f"<p>{p}</p>" for p in parts)
            css = make_css(u, align, font_css)
            rot = u.rotate
            if u.verbatim:
                # unchanged text: it fits by definition, only our font metrics can differ a bit
                spare, scale = page.insert_htmlbox(rect, body_html, css=css, scale_low=0.6,
                                                   archive=archive, rotate=rot)
                if spare < 0:
                    spare, scale = page.insert_htmlbox(rect, body_html, css=css, scale_low=0,
                                                       archive=archive, rotate=rot)
                stats["vectorized"] += 1
                # reserve the space the redrawn text really takes (our font may be wider)
                txt = para_text(u.lines)
                font = {"serif": "tiro", "monospace": "cour"}.get(u.lines[0].family, "helv")
                w = pymupdf.get_text_length(txt, fontname=font, fontsize=u.size * scale) + 1
                if align == "right":
                    ext = pymupdf.Rect(rect.x1 - w, u.rect.y0, rect.x1, u.rect.y1)
                elif align == "center":
                    cx = (rect.x0 + rect.x1) / 2
                    ext = pymupdf.Rect(cx - w / 2, u.rect.y0, cx + w / 2, u.rect.y1)
                else:
                    ext = pymupdf.Rect(rect.x0, u.rect.y0, rect.x0 + w, u.rect.y1)
                placed.append(ext | u.rect)
                if debug:
                    page.draw_rect(rect, color=(0, 0.7, 0), width=0.4, dashes="[2] 0")
                continue
            spare, scale = page.insert_htmlbox(rect, body_html, css=css, scale_low=args.min_scale,
                                               archive=archive, rotate=rot)
            if spare < 0:
                spare, scale = page.insert_htmlbox(rect, body_html, css=css, scale_low=0,
                                                   archive=archive, rotate=rot)
                stats["overflow"] += 1
                log.warning("p%d: text needed scale %.2f (< min %.2f): %.60r", pno + 1, scale,
                            args.min_scale, para_text(u.lines))
            elif scale < 0.97:
                stats["shrunk"] += 1
            stats["boxes"] += 1
            used = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1 - max(0.0, spare))
            if rot in (0, 180):
                placed.append(used)
            else:
                placed.append(rect)
            if debug:
                page.draw_rect(rect, color=(1, 0, 0), width=0.4, dashes="[2] 0")
                page.draw_rect(u.rect, color=(0, 0.6, 1), width=0.3)
    doc.save(out_path, garbage=3, deflate=True)
    return stats


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--langs", nargs="+", default=["de", "es", "fr"], help="target language codes")
    ap.add_argument("--source-lang", default=None, help="source language code (default: auto-detect)")
    ap.add_argument("--out-dir", default=None, help="output directory (default: next to input)")
    ap.add_argument("--translator", choices=["cloud", "gemini", "free", "pseudo"], default="cloud")
    ap.add_argument("--translate-key", default=os.environ.get("GOOGLE_TRANSLATE_KEY") or os.environ.get("GOOGLE_API_KEY"),
                help="Cloud Translation API key (--translator cloud)")
    ap.add_argument("--gemini-key", default=os.environ.get("GEMINI_API_KEY"),
                help="Gemini API key from Google AI Studio (--translator gemini)")
    ap.add_argument("--gemini-model", default="gemini-2.5-flash", help="Gemini model name")
    ap.add_argument("--ocr", choices=["vision", "tesseract", "none"], default="vision")
    ap.add_argument("--vision-key", default=os.environ.get("GOOGLE_VISION_KEY") or os.environ.get("GOOGLE_API_KEY"))
    ap.add_argument("--keep", action="append", default=[], help="term never to translate (repeatable)")
    ap.add_argument("--min-scale", type=float, default=0.75, help="smallest allowed font scale (default 0.75)")
    ap.add_argument("--max-grow", type=float, default=1.0,
                    help="how much a text box may grow downward, as a multiple of its height (default 1.0)")
    ap.add_argument("--margin", type=float, default=20.0, help="page margin text may not enter (pt)")
    ap.add_argument("--min-conf", type=float, default=0.6, help="minimum OCR confidence")
    ap.add_argument("--min-text-pt", type=float, default=4.0, help="ignore OCR text smaller than this (pt)")
    ap.add_argument("--min-image-pt", type=float, default=60.0, help="ignore images narrower than this (pt)")
    ap.add_argument("--keep-raster-numbers", action="store_true",
                    help="do NOT redraw untranslated raster text (numbers, dimensions) as vector text")
    ap.add_argument("--vector-min-conf", type=float, default=0.9,
                    help="min OCR confidence of EVERY character to redraw raster numbers as vectors "
                         "(default 0.9); below that the original pixels are kept")
    ap.add_argument("--vector-min-px", type=float, default=9.0,
                    help="min text height in the image's own pixels to redraw raster numbers as "
                         "vectors (default 9); smaller = too low-res to trust, pixels are kept")
    ap.add_argument("--ocr-font", default="sans-serif", help="CSS family for text replacing OCR text")
    ap.add_argument("--ocr-size-factor", type=float, default=1.0, help="fudge factor for the font size estimated from OCR boxes")
    ap.add_argument("--font-file", default=None, help="TTF/OTF used for all new text (e.g. NotoSans for Cyrillic)")
    ap.add_argument("--cache-dir", default=".pdf_translate_cache", help="OCR + translation cache ('' to disable)")
    ap.add_argument("--debug", action="store_true", help="draw layout boxes into the output")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if args.translator == "cloud" and not args.translate_key:
        ap.error("--translate-key (or GOOGLE_TRANSLATE_KEY) is required for --translator cloud")
    if args.translator == "gemini" and not args.gemini_key:
        ap.error("--gemini-key (or GEMINI_API_KEY) is required for --translator gemini")
    if args.ocr == "vision" and not args.vision_key:
        ap.error("--vision-key (or GOOGLE_VISION_KEY) is required for --ocr vision (or use --ocr none)")

    ocr = None
    if args.ocr == "vision":
        ocr = VisionOCR(args.vision_key, args.source_lang)
    elif args.ocr == "tesseract":
        ocr = TesseractOCR()

    font_css, archive = "", None
    if args.font_file:
        archive = pymupdf.Archive(os.path.dirname(os.path.abspath(args.font_file)))
        font_css = f"@font-face {{font-family: userfont; src: url({os.path.basename(args.font_file)});}} "

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.pdf))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.pdf))[0]

    log.info("Analysing %s ...", args.pdf)
    doc = pymupdf.open(args.pdf)
    pages, replaced = analyze(doc, ocr, args)

    segments = set()
    for pd in pages:
        for u in pd.units:
            for _, body in unit_segments(u):
                if needs_translation(body, args.keep):
                    segments.add(body)
    segments = sorted(segments)
    log.info("%d unique text segments, %d images with text", len(segments), len(replaced))

    cache_path = os.path.join(args.cache_dir, "translations.json") if args.cache_dir else None
    cache = {}
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf8") as f:
            cache = json.load(f)

    for lang in args.langs:
        ck = f"{args.translator}:{args.source_lang or 'auto'}:{lang}"
        kw = dict(target=lang, source=args.source_lang, keep_terms=args.keep, cache=cache.setdefault(ck, {}))
        if args.translator == "cloud":
            tr = CloudTranslator(args.translate_key, **kw)
        elif args.translator == "gemini":
            tr = GeminiTranslator(args.gemini_key, model=args.gemini_model, **kw)
        elif args.translator == "free":
            tr = FreeTranslator(**kw)
        else:
            tr = PseudoTranslator(**kw)
        log.info("[%s] translating ...", lang)
        tmap = tr.translate_many(segments)
        if cache_path and args.translator != "pseudo":
            os.makedirs(args.cache_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=0)
        out_path = os.path.join(out_dir, f"{stem}_{lang}.pdf")
        stats = render(args.pdf, pages, replaced, tmap, out_path, args, font_css, archive, args.debug)
        log.info("[%s] wrote %s  (%d boxes, %d shrunk, %d below min scale, %d raster numbers "
                 "redrawn as vectors)", lang, out_path,
                 stats["boxes"], stats["shrunk"], stats["overflow"], stats["vectorized"])


if __name__ == "__main__":
    main()

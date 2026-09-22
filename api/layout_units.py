"""Groups extracted text into layout UNITS -- the thing that actually gets
translated and re-laid-out as a whole.

extract.py emits one text_box per PyMuPDF span (kept for the JSON schema /
future editor), but a span is the wrong granularity to translate or fit:
one wrapped sentence is several spans on several lines, and translating
and shrinking each piece independently is what produced every
layout bug this project hit (mixed font sizes inside one list/heading,
uneven word splits between lines, words split mid-word, text bleeding
past its column). Ported from `working python script/pdf_translate.py`,
which avoids all of those by construction:

  spans -> LINES (every span of one PyMuPDF line, joined)
        -> CLUSTERS (vertically adjacent lines with the same size/weight/
           colour and a shared left edge or centre)
        -> PARAGRAPHS (split where a line ends a sentence or starts a
           bullet; soft-wrapped lines are joined, hyphenation removed)
        -> UNITS: a free-standing cluster is one unit that is re-flowed
           as a whole box; a table-like cluster (several of its rows have
           row-aligned neighbour text) is one unit per paragraph/cell.

Clusters containing nothing translatable (numbers, dimensions, part
codes) produce no unit at all -- reinsert.py never touches them, so they
stay byte-identical to the source.

Also extracts the page's drawn straight line segments (table rules,
dashed frames), which reinsert.py uses as hard borders a translated box
may not grow across.
"""

import re
from collections import defaultdict
from statistics import median

import pymupdf

URL_RE = re.compile(
    r"(?:https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|"
    r"\b(?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|de|fr|es|eu|co|uk|lv|info|biz|app)\b)",
    re.I,
)
# Tokens that alone don't make text worth translating. Deliberately NOT
# "pc"/"pcs": those do localize ("Stk.", "pzas.") -- the glossary maps them.
UNIT_WORDS = {
    "mm", "cm", "dm", "m", "km", "m²", "m2", "m³", "m3", "kg", "g", "t",
    "ft", "in", "ft²", "eur", "usd", "gbp", "kw", "w", "v", "l", "ml",
}
BULLET_RE = re.compile(r"^([●•▪■◦·‣∙\-–—*]\s+)")
_SENTENCE_END = tuple(".!?:;")

# Page margin (pt) a translated box may not grow into.
PAGE_MARGIN = 20.0

_SERIF_MARKERS = (
    "times", "georgia", "garamond", "bodoni", "cambria", "constantia",
    "palatino", "book", "minion", "caslon", "didot", "baskerville",
    "century", "charter", "roman", "serif",
)
_MONO_MARKERS = ("courier", "consolas", "menlo", "monaco", "mono")


def needs_translation(text):
    """False for pure numbers / dimensions / units / part codes / URLs."""
    s = URL_RE.sub(" ", text)
    words = re.findall(r"[^\W\d_]{2,}", s)
    return any(w.lower() not in UNIT_WORDS for w in words)


def split_prefix(text):
    """('• ', 'rest') for a bulleted paragraph, ('', text) otherwise."""
    m = BULLET_RE.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def para_text(lines):
    out = ""
    for ln in lines:
        t = ln["text"].strip()
        if not out:
            out = t
        elif re.search(r"[^\W\d_]-$", out) and t[:1].islower():
            out = out[:-1] + t  # de-hyphenate
        else:
            out += " " + t
    return re.sub(r"\s+", " ", out)


def _family(font_name):
    n = (font_name or "").lower()
    if any(k in n for k in _MONO_MARKERS):
        return "monospace"
    if any(k in n for k in _SERIF_MARKERS) and "sans" not in n:
        return "serif"
    return "sans-serif"


def _rect_union(rects):
    r = pymupdf.Rect(rects[0])
    for other in rects[1:]:
        r |= pymupdf.Rect(other)
    return r


def _y_overlap(a, b, tol=1.0):
    return a.y0 < b.y1 - tol and a.y1 > b.y0 + tol


def _color_dist(a, b):
    return max(abs(((a >> s) & 255) - ((b >> s) & 255)) for s in (16, 8, 0)) / 255


def _style(box):
    """(bold, italic) of one text_box, from PyMuPDF flags or the font name."""
    flags = box["font"].get("flags", 0)
    lower = (box["font"]["name"] or "").lower()
    bold = bool(flags & 16) or any(k in lower for k in ("bold", "black", "heavy"))
    italic = bool(flags & 2) or any(k in lower for k in ("italic", "oblique"))
    return bold, italic


def page_lines(page_json):
    """Rebuilds visual lines from a page's text_boxes.

    Vector boxes are grouped back into their PyMuPDF line via the
    "p{page}-b{block}-l{line}-s{span}" id; each OCR box already is one
    visual line. Returns plain dicts (JSON-friendly) with a pymupdf.Rect
    under "rect_obj" for geometry work.
    """
    grouped = defaultdict(list)
    order = []
    for tb in page_json["text_boxes"]:
        if tb.get("source") == "ocr":
            key = tb["id"]
        else:
            key = tb["id"].rsplit("-s", 1)[0]
        if key not in grouped:
            order.append(key)
        grouped[key].append(tb)

    lines = []
    for key in order:
        boxes = grouped[key]
        visible = [b for b in boxes if b["text"].strip()]
        if not visible:
            continue
        # A line that opens with a differently-styled CODE span ("D1-A."
        # bold, then "1 pc." regular -- common in part lists) keeps that
        # style for the code only; the rest of the line decides the
        # unit's style. See reinsert.py's paragraph HTML.
        lead = None
        styled = visible
        if len(visible) >= 2:
            first_style = _style(visible[0])
            rest_styles = [_style(b) for b in visible[1:]]
            rest_style = max(set(rest_styles), key=rest_styles.count)
            if first_style != rest_style and not needs_translation(visible[0]["text"]):
                lead = {
                    "text": visible[0]["text"].strip(),
                    "bold": first_style[0],
                    "italic": first_style[1],
                }
                styled = visible[1:]
        main = max(styled, key=lambda b: len(b["text"].strip()))
        font_name = main["font"]["name"]
        bold, italic = _style(main)
        source = main.get("source", "vector")
        angle = main["rotation_angle"]
        lines.append({
            "key": key,
            "box_ids": [b["id"] for b in boxes],
            "raw_text": "".join(b["text"] for b in boxes),
            "text": re.sub(r"\s+", " ", "".join(b["text"] for b in boxes)).strip(),
            "rect": list(_rect_union([b["bbox"] for b in visible])),
            "size": main["font"]["size"],
            "color": main["font"]["color"],
            "bold": bold,
            "italic": italic,
            "lead": lead,
            "family": _family(font_name),
            "angle": angle,
            "source": source,
            # OCR lines only cluster with lines of the same image AND the
            # same Vision paragraph.
            "group": (
                f"{key.rsplit('-l', 1)[0]}/{main['ocr']['group']}" if source == "ocr" else None
            ),
            "bounds": main.get("image_bbox"),
            # OCR only: image xref + word boxes to erase, lowest confidence.
            "ocr": main.get("ocr"),
            "baseline": main["origin"][1],
        })

    # Some PDFs draw the same text twice at the same spot (layering / faux
    # bold -- the real sample's footer does). One redaction removes both
    # copies; translating and drawing both would double the new text.
    unique = []
    for ln in lines:
        twin = next(
            (u for u in unique if u["text"] == ln["text"]
             and max(abs(a - b) for a, b in zip(u["rect"], ln["rect"])) < 0.5),
            None,
        )
        if twin is not None:
            twin["box_ids"] += ln["box_ids"]
        else:
            unique.append(ln)
    return unique


def extract_segments(page, min_len=25.0, gap=5.0):
    """Horizontal / vertical drawn lines, merged so dashed borders become one
    segment. Returns ([(x, y0, y1)], [(y, x0, x1)])."""
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
                        out.append([float(k), s, e])
                    s, e = a, b
            if e - s >= min_len:
                out.append([float(k), s, e])
        return out

    return merge(vb), merge(hb)


def _is_horizontal(line):
    return line["angle"] == 0.0


def _cluster_lines(lines):
    horiz = [l for l in lines if _is_horizontal(l)]
    other = [[l] for l in lines if not _is_horizontal(l)]
    horiz.sort(key=lambda l: (l["rect_obj"].y0, l["rect_obj"].x0))
    clusters = []
    for ln in horiz:
        target = None
        for c in reversed(clusters):
            last = c[-1]
            if last["group"] != ln["group"] or last["source"] != ln["source"]:
                continue
            s = max(last["size"], ln["size"])
            if abs(last["size"] - ln["size"]) > 0.2 * s:
                continue
            if ln["source"] == "vector" and last["bold"] != ln["bold"]:
                continue
            if _color_dist(last["color"], ln["color"]) > 0.3:
                continue
            lr, r = last["rect_obj"], ln["rect_obj"]
            gap = r.y0 - lr.y1
            if gap < -0.3 * s or gap > 0.8 * s:
                continue
            left_ok = abs(r.x0 - lr.x0) <= 0.8 * s
            center_ok = abs((r.x0 + r.x1) - (lr.x0 + lr.x1)) / 2 <= 0.8 * s
            if not (left_ok or center_ok):
                continue
            target = c
            break
        if target is not None:
            target.append(ln)
        else:
            clusters.append([ln])
    return clusters + other


def _split_paragraphs(cluster):
    paras = [[cluster[0]]]
    for prev, cur in zip(cluster, cluster[1:]):
        p, c = prev["text"].rstrip(), cur["text"].lstrip()
        soft = (not p.endswith(_SENTENCE_END) and c[:1].islower()) or p.endswith((",", "-"))
        if soft and not BULLET_RE.match(c):
            paras[-1].append(cur)
        else:
            paras.append([cur])
    return paras


def _right_limit(rect, bounds, vsegs, pad):
    right = bounds.x1
    for x, y0, y1 in vsegs:
        if x >= rect.x1 - 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            right = min(right, x - pad)
    return max(right, rect.x1)


def _left_limit(rect, bounds, vsegs, pad):
    left = bounds.x0
    for x, y0, y1 in vsegs:
        if x <= rect.x0 + 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            left = max(left, x + pad)
    return min(left, rect.x0)


def _rotate(angle):
    if angle is None:
        return None
    a = round(angle) % 360
    return a if a in (0, 90, 180, 270) and abs(angle - round(angle)) < 1e-6 else None


def _major(lines, key):
    vals = [l[key] for l in lines]
    return max(set(vals), key=vals.count)


def _make_unit(unit_id, paras, bounds):
    lines = [ln for p in paras for ln in p]
    size = median(l["size"] for l in lines)
    pitches = [b["rect_obj"].y0 - a["rect_obj"].y0 for a, b in zip(lines, lines[1:])
               if b["rect_obj"].y0 > a["rect_obj"].y0]
    line_height = max(1.0, min(1.6, median(pitches) / size)) if pitches else 1.2
    index = {id(l): i for i, l in enumerate(lines)}
    return {
        "id": unit_id,
        "source": lines[0]["source"],
        "lines": [{k: v for k, v in l.items() if k != "rect_obj"} for l in lines],
        "paragraphs": [
            {"line_indexes": [index[id(l)] for l in p], "text": para_text(p), "lead": p[0]["lead"]}
            for p in paras
        ],
        "rect": list(_rect_union([l["rect"] for l in lines])),
        "bounds": list(bounds),
        "size": size,
        "color": _major(lines, "color"),
        "bold": _major(lines, "bold"),
        "italic": _major(lines, "italic"),
        "family": lines[0]["family"],
        "rotate": _rotate(lines[0]["angle"]),
        "line_height": line_height,
        "pipeline": {
            "classification": None,
            "target_lang": None,
            "translated_paragraphs": None,
            "needs_review": False,
            "scale": None,
            "overflow": None,
        },
    }


def build_units(page_json, vsegs):
    """Units for one extract.py page dict (see module docstring)."""
    page_rect = pymupdf.Rect(0, 0, page_json["width"], page_json["height"])
    page_bounds = page_rect + (PAGE_MARGIN, PAGE_MARGIN, -PAGE_MARGIN, -PAGE_MARGIN)
    lines = page_lines(page_json)
    for ln in lines:
        ln["rect_obj"] = pymupdf.Rect(ln["rect"])

    def bounds_for(ln):
        if ln["bounds"]:
            return pymupdf.Rect(ln["bounds"]) + (1, 1, -1, -1)
        return page_bounds

    units = []
    prefix = f"p{page_json['page_index']}-u"

    def add(paras, bounds):
        units.append(_make_unit(f"{prefix}{len(units)}", paras, bounds))

    for cluster in _cluster_lines(lines):
        paras = _split_paragraphs(cluster)
        if not any(needs_translation(para_text(p)) for p in paras):
            continue
        bounds = bounds_for(cluster[0])
        own = set(map(id, cluster))
        others = [l["rect_obj"] for l in lines if id(l) not in own]
        crect = _rect_union([l["rect"] for l in cluster])
        flow = len(paras) > 1
        if flow:
            # Table-like = at least two rows of the block have their own
            # row-aligned neighbour text. One stray label beside it is fine.
            seg_right = _right_limit(crect, bounds, vsegs, 2)
            seg_left = _left_limit(crect, bounds, vsegs, 2)
            rows_with_neighbours = 0
            for ln in cluster:
                lr = ln["rect_obj"]
                for r in others:
                    if r.height > 1.6 * lr.height or not _y_overlap(r, lr):
                        continue
                    if (r.x0 >= crect.x1 - 1 and r.x0 < seg_right) or \
                            (r.x1 <= crect.x0 + 1 and r.x1 > seg_left):
                        rows_with_neighbours += 1
                        break
            flow = rows_with_neighbours < 2
        if flow or len(paras) == 1:
            add(paras, bounds)
        else:
            for p in paras:
                if needs_translation(para_text(p)):
                    add([p], bounds)
    return units

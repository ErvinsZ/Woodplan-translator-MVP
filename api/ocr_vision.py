"""Google Cloud Vision OCR extractor: whole flattened pages AND individual
raster images with baked-in text on an otherwise-normal page.

extract.py's PyMuPDF-based extraction only finds REAL vector text. Two
distinct cases need OCR instead, and this module handles both:

  1. A whole page that's actually a scanned image or an export-to-image
     "flattened" plan -- no real text objects anywhere on it, per
     CLAUDE.md's original architecture note. `ocr_page` OCRs the whole
     rendered page.
  2. A page that DOES have real vector text (a copyright footer, part
     labels, etc.) but ALSO contains one or more embedded raster images
     that separately have text baked into their own pixels -- e.g. a
     materials/cost table exported as a flat image by whatever tool made
     it, unlike this project's original sample PDF, where CLAUDE.md's
     core finding was that diagrams never have baked-in text. Found on a
     real seller's PDF (a full materials table with 20+ rows, baked
     entirely into one embedded image, silently invisible to plain
     vector extraction since the PAGE as a whole wasn't empty -- the
     whole-page fallback above never triggered). `ocr_image_regions`
     OCRs each embedded image's OWN RAW PIXEL DATA individually
     (extracted directly via its PDF xref, `_ocr_raw_image`) and returns
     extra text_boxes to ADD to that page's normal vector-extracted
     ones, not replace them. Deliberately NOT a `get_pixmap(clip=...)`
     render of that image's page-space region -- found the hard way on
     the same real seller PDF that rendering a region that way also
     renders any real vector text positioned on top of or near that
     image, causing Vision to re-detect text ALREADY correctly captured
     by normal vector extraction; both got drawn, independently, at
     slightly different positions, producing visible doubled/ghosted
     text in the real output. See `ocr_image_regions`'s own docstring
     for the full story.

Both reconstruct the SAME JSON schema extract.py produces, so
classify_translate.py needs zero changes to work with any source -- it
only ever sees text_boxes with pipeline/bbox/origin/font/rotation fields,
never which extractor produced them.

reinsert.py DOES need to know: an OCR-sourced box has no underlying vector
text to redact (the English is baked into image pixels), so "erasing" the
original means painting directly over those pixels instead of the normal
redaction-annotation path, which explicitly never touches image content.
Every text_box this module emits carries `"source": "ocr"` for exactly that
branch -- see reinsert.py's reinsert_page.

Talks to the Vision REST API directly via urllib with a plain API key, not
a service-account/OAuth2 flow -- same hand-rolled-REST pattern as
gemini_translate.py and blob.py; a service-account JWT flow would need a
crypto library just to sign requests. (It was originally avoided over
compiled-extension crashes that turned out to be a local Python-version
mismatch -- see CLAUDE.md -- but a plain key is still simpler.) The key
must be in the Vercel project env, not only `.env.local`: `vercel dev`'s
Python functions don't read `.env.local`.

Besides text, this module returns what reinsert.py needs to erase it: each
OCR line's word boxes in the source image's pixels (inpainting, see
image_erase.py), Vision paragraph ids (layout grouping), the lowest symbol
confidence, a baseline and a text colour measured from the pixels, and the
straight lines drawn inside each image (`raster_segments`, layout borders).
"""

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

import certifi
import numpy as np
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geometry import rotation_angle as _rotation_angle

_API_URL = "https://vision.googleapis.com/v1/images:annotate"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_REQUEST_TIMEOUT_SECONDS = 60

# Render scale for the page image sent to Vision -- 2x page points (i.e.
# ~144 DPI for a standard 72-DPI PDF user space) balances OCR accuracy
# against request payload size. Vision bounding-box coordinates come back
# in this same pixel space and are divided by this factor to get back to
# PDF points.
_RENDER_SCALE = 2.0

# Break types that end a visual LINE, not just a word, within Vision's
# word/symbol hierarchy -- see _lines_from_annotation's docstring for how
# these drive line reconstruction (HYPHEN also ends a line).
_LINE_END_BREAKS = {"LINE_BREAK", "EOL_SURE_SPACE"}
# Characters reaching below the baseline -- a word containing one has a
# taller ink box relative to its em size (see _text_boxes_from_annotation).
_DESCENDER_RE = re.compile(r"[gjpqyQ(),;\[\]{}|/]")

# geometry.rotation_angle's tolerance for "close enough to horizontal/
# vertical to snap to exactly 0/90" -- much wider than the 0.5-degree
# default, which is tuned for PyMuPDF's mathematically-exact vector line
# directions. Vision's word bounding boxes are fitted around detected
# ink, not font metrics, and a real API response against a real,
# entirely-horizontal page showed up to 6.34 degrees of pure detection
# jitter (confirmed directly, not estimated) -- without a wider
# tolerance here, a third of that real page's lines got flagged
# `is_rotated: True` from noise alone, which matters a lot downstream:
# reinsert.py's rotated-text path skips the entire overflow/shrink
# system. 8.0 clears that observed noise ceiling with real margin while
# staying well below any genuine diagram angle-callout seen so far in
# real vector data (26/45/64/90 degrees).
_OCR_ROTATION_EPSILON_DEGREES = 8.0

# Skip OCR-ing an embedded image smaller than this (page points squared)
# on an otherwise-normal page -- a small logo/watermark/icon isn't worth
# a Vision API call. Every real content image with actual dimension
# labels found on a real seller PDF so far is far larger than this
# (smallest was ~15,600 sq pt); a generic small badge/icon is typically
# well under 2,500 sq pt (e.g. 50x50pt). Purely a cost optimization --
# an image with no text just returns an empty annotation either way, see
# _ocr_region.
_MIN_IMAGE_OCR_AREA = 2500.0

# Skip an embedded image if this much of its own area is already covered
# by other images on the same page already processed. Real seller PDFs
# can have embedded images that substantially overlap each other in page
# space -- found directly on a real page: a "zoomed detail" callout image
# positioned on top of the main illustration, overlapping three of that
# page's other four images. OCR-ing each independently detected much of
# the SAME real-world text more than once, each redrawn at its own
# slightly different detected position -- visible as literal doubled/
# ghosted text in the real rendered output (e.g. two overlapping "4335
# mm" labels, one rotated differently from the other). 0.5 treats
# "mostly covered by images already claimed" as a likely duplicate/
# overlay rather than genuinely distinct content.
_MAX_IMAGE_OVERLAP_RATIO = 0.5


class _VisionError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Vision API error {code}: {message}")
        self.code = code


def _api_key():
    api_key = os.environ.get("GOOGLE_VISION_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_VISION_API_KEY is not set")
    return api_key


def _call_vision(image_bytes):
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }
    url = f"{_API_URL}?key={_api_key()}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, context=_SSL_CONTEXT, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise _VisionError(exc.code, exc.read().decode()) from exc

    result = body["responses"][0]
    if "error" in result:
        raise _VisionError(result["error"].get("code"), result["error"].get("message"))
    return result.get("fullTextAnnotation")


def _word_info(word):
    """(text, break_type, min_symbol_confidence, vertices) for one Vision word.
    break_type is whatever break follows its LAST symbol, or None."""
    chars = []
    break_type = None
    confs = []
    for symbol in word.get("symbols", []):
        chars.append(symbol.get("text", ""))
        detected = symbol.get("property", {}).get("detectedBreak")
        break_type = detected["type"] if detected else None
        confs.append(symbol.get("confidence", word.get("confidence", 1.0)))
    min_conf = min(confs) if confs else word.get("confidence", 1.0)
    return "".join(chars), break_type, min_conf, _poly_vertices(word.get("boundingBox", {}))


def _poly_vertices(bounding_poly):
    return bounding_poly.get("vertices") or bounding_poly.get("normalizedVertices") or []


def _box(vertices):
    xs = [v.get("x", 0) for v in vertices] or [0]
    ys = [v.get("y", 0) for v in vertices] or [0]
    return (min(xs), min(ys), max(xs), max(ys))


def _split_on_gaps(words, group, hyphen):
    """One Vision line -> one or more lines, split where the horizontal gap
    between words is large. Vision regularly reports a whole table row
    (every column) as ONE line; each cell has to be its own text line or
    the row can't be laid out, and the gap is the only signal for that."""
    heights = sorted(w["box"][3] - w["box"][1] for w in words)
    h = heights[len(heights) // 2] or 1
    segments, cur = [], [words[0]]
    for w in words[1:]:
        if w["box"][0] - cur[-1]["box"][2] > 1.2 * h:
            segments.append(cur)
            cur = [w]
        else:
            cur.append(w)
    segments.append(cur)
    out = []
    for i, seg in enumerate(segments):
        text = "".join(w["text"] + w["sep"] for w in seg).strip()
        last = i == len(segments) - 1
        if hyphen and last and not text.endswith("-"):
            text += "-"  # layout_units.para_text de-hyphenates across lines
        out.append({
            "text": text,
            "words": [w["box"] for w in seg],
            "word_texts": [w["text"] for w in seg],
            # first word's top edge gives the writing direction
            "vertices": seg[0]["vertices"],
            "group": group,
            "min_conf": min(w["conf"] for w in seg),
        })
    return out


def _lines_from_annotation(annotation):
    """Reconstructs one entry per VISUAL LINE from Vision's word/symbol
    hierarchy, matching extract.py's one-text-box-per-original-line
    granularity.

    Vision has no explicit "line" grouping -- only a break-type hint on
    each word's last symbol (SPACE/SURE_SPACE: more of this line follows;
    EOL_SURE_SPACE/LINE_BREAK: this line ends here; HYPHEN: the line ends
    in a hyphenated word-wrap). Each line carries its Vision paragraph as
    `group` (lines from different paragraphs never merge into one layout
    unit), its word boxes in the OCR'd image's pixel space (erasure), and
    the lowest symbol confidence in it.
    """
    lines = []
    for pi, page in enumerate(annotation.get("pages", [])):
        for bi, block in enumerate(page.get("blocks", [])):
            for qi, paragraph in enumerate(block.get("paragraphs", [])):
                group = f"{pi}.{bi}.{qi}"
                cur = []
                words = paragraph.get("words", [])
                for i, word in enumerate(words):
                    text, break_type, conf, vertices = _word_info(word)
                    if not text:
                        continue
                    cur.append({
                        "text": text,
                        "sep": " " if break_type in ("SPACE", "SURE_SPACE") else "",
                        "conf": conf,
                        "box": _box(vertices),
                        "vertices": vertices,
                    })
                    if i == len(words) - 1 or break_type in _LINE_END_BREAKS or break_type == "HYPHEN":
                        lines += _split_on_gaps(cur, group, break_type == "HYPHEN")
                        cur = []
                if cur:
                    lines += _split_on_gaps(cur, group, False)
    return lines


def _ink_rows(pixels, words, pixel_scale):
    """(top, bottom) of the rows that actually contain ink inside a line's
    word boxes, in the words' pixel space, or None if nothing is found.
    Vision's word boxes are NOT a reliable height: same-size text in one
    real table came back with 10px boxes on one row and 13-16px on
    another, which made those rows' translations render 50% too large."""
    H, W = pixels.shape[:2]
    x0 = int(max(0, min(w[0] for w in words) * pixel_scale))
    # No margin: Vision's boxes already enclose the ink, and in a tight
    # table a margin reaches into the next row's letters.
    y0 = int(max(0, min(w[1] for w in words) * pixel_scale))
    x1 = int(min(W, max(w[2] for w in words) * pixel_scale))
    y1 = int(min(H, max(w[3] for w in words) * pixel_scale))
    region = pixels[y0:y1, x0:x1].astype(np.float32)
    if region.shape[0] < 3 or region.shape[1] < 3:
        return None
    ring = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    ink = np.linalg.norm(region - np.median(ring, axis=0), axis=2) > 60
    rows = np.nonzero(ink.mean(axis=1) > 0.01)[0]
    if len(rows) < 2:
        return None
    return ((y0 + rows[0]) / pixel_scale, (y0 + rows[-1] + 1) / pixel_scale)


def _line_color(pixels, words, scale_x, scale_y):
    """Text colour of one OCR line, sampled from the OCR'd image: pixels
    clearly away from the local background (median of the region's
    border), taking the most distant 40%. `pixels` is an HxWx3 uint8
    array in the same pixel space as `words` scaled by scale_x/scale_y."""
    H, W = pixels.shape[:2]
    x0 = int(max(0, min(w[0] for w in words) * scale_x - 2))
    y0 = int(max(0, min(w[1] for w in words) * scale_y - 2))
    x1 = int(min(W, max(w[2] for w in words) * scale_x + 2))
    y1 = int(min(H, max(w[3] for w in words) * scale_y + 2))
    region = pixels[y0:y1, x0:x1].astype(float)
    if region.size == 0:
        return 0
    ring = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    bg = np.median(ring, axis=0)
    dist = np.linalg.norm(region - bg, axis=2)
    ink = region[dist > 30]
    if len(ink) == 0:
        return 0
    d = np.linalg.norm(ink - bg, axis=1)
    r, g, b = ink[d >= np.quantile(d, 0.6)].mean(axis=0)
    return (int(r) << 16) | (int(g) << 8) | int(b)


def _text_boxes_from_annotation(annotation, id_prefix, scale_x, scale_y, offset_x=0.0,
                                offset_y=0.0, xref=None, pixels=None, pixel_scale=1.0):
    """scale_x/scale_y: OCR'd-image pixel -> page point factors (per axis:
    an embedded image's placement can stretch its axes differently).
    offset_x/offset_y: page-space top-left of the OCR'd region. `pixels`
    (optional, HxWx3 array of the OCR'd image at `pixel_scale` x its
    OCR'd resolution) is used to sample each line's text colour.

    Font size: OCR boxes hug the ink, which is SMALLER than the em size --
    em ~= box height / 0.92 for a line with descenders (g, p, y, ...), /
    0.72 without. Sizes within 0.7x-1.35x of the image's median are
    snapped to that median so one table/list doesn't come out mixed.
    The height is measured from the INK rows inside the line (_ink_rows)
    when pixels are available, not from Vision's word boxes.
    (The old `height * 0.75` rendered OCR text at roughly 55-70% of its
    real size.)
    """
    lines = [l for l in _lines_from_annotation(annotation) if l["text"].strip()]
    if not lines:
        return []
    ems, baselines = [], []
    for l in lines:
        desc = bool(_DESCENDER_RE.search(l["text"]))
        top = min(w[1] for w in l["words"])
        bottom = max(w[3] for w in l["words"])
        ink = _ink_rows(pixels, l["words"], pixel_scale) if pixels is not None else None
        if ink is not None:
            top, bottom = ink
        h = max((bottom - top) * scale_y, 0.5)
        em = h / (0.92 if desc else 0.72)
        ems.append(em)
        page_bottom = bottom * scale_y + offset_y
        # Ink bottom IS the baseline without descenders; with them, the
        # baseline sits about 0.2 em higher.
        baselines.append(page_bottom - 0.2 * em if desc else page_bottom)
    em_med = sorted(ems)[len(ems) // 2]

    text_boxes = []
    for line_no, (l, em, baseline) in enumerate(zip(lines, ems, baselines)):
        size = em_med if 0.7 * em_med <= em <= 1.35 * em_med else em
        x0p, y0p, x1p, y1p = _box([{"x": v, "y": u} for w in l["words"] for v, u in ((w[0], w[1]), (w[2], w[3]))])
        x0, y0 = x0p * scale_x + offset_x, y0p * scale_y + offset_y
        x1, y1 = x1p * scale_x + offset_x, y1p * scale_y + offset_y

        # Angle of the first word's top edge (Vision's clockwise-from-top-
        # left vertex order) -- same convention geometry.rotation_angle
        # expects. dx=0 is the CORRECT value for genuinely vertical text,
        # not a degenerate case (do NOT use a truthy fallback like `dx or 1`).
        v = l["vertices"]
        if len(v) >= 2 and (v[1].get("x", 0) != v[0].get("x", 0) or v[1].get("y", 0) != v[0].get("y", 0)):
            line_dir = (v[1].get("x", 0) - v[0].get("x", 0), v[1].get("y", 0) - v[0].get("y", 0))
        else:
            line_dir = (1, 0)
        rotation_angle = _rotation_angle(line_dir, _OCR_ROTATION_EPSILON_DEGREES)
        color = _line_color(pixels, l["words"], pixel_scale, pixel_scale) if pixels is not None else 0

        text_boxes.append({
            "id": f"{id_prefix}{line_no}",
            "text": l["text"],
            "bbox": [x0, y0, x1, y1],
            "origin": [x0, baseline],
            "font": {"name": "", "size": round(size, 2), "color": color, "flags": 0},
            "line_dir": list(line_dir),
            "is_rotated": rotation_angle != 0.0,
            "rotation_angle": rotation_angle,
            "source": "ocr",
            "ocr": {
                # Embedded image this text is baked into (None for a whole
                # rendered page) + word boxes in that image's own pixels --
                # reinsert.py erases exactly these by inpainting.
                "xref": xref,
                "words_px": [list(w) for w in l["words"]],
                "words_pt": [
                    [w[0] * scale_x + offset_x, w[1] * scale_y + offset_y,
                     w[2] * scale_x + offset_x, w[3] * scale_y + offset_y]
                    for w in l["words"]
                ],
                "group": l["group"],
                "min_conf": round(l["min_conf"], 3),
            },
            "pipeline": {
                "classification": None,
                "target_lang": None,
                "translated_text": None,
                "needs_review": False,
                "overflow": None,
                "used_font_size": None,
            },
        })
    return text_boxes


def _ocr_region(fitz_page, clip_rect, id_prefix):
    """Renders `clip_rect` (a pymupdf.Rect in PAGE space, or None for the
    whole page) to an image, OCRs it, returns a list of text_boxes with
    coordinates already mapped back into page space regardless of which
    region was actually rendered.

    Only used for a WHOLE-page OCR (ocr_page) -- a genuinely flattened
    page has no vector content to worry about contaminating the render.
    Do NOT use this for a single embedded image on an otherwise-normal
    page: `get_pixmap` renders the page's full visual content within the
    clip rect, including any real vector text drawn on top of or near
    that image, not just the image's own pixels -- see
    ocr_image_regions's docstring for the real bug this caused.
    """
    pix = fitz_page.get_pixmap(matrix=pymupdf.Matrix(_RENDER_SCALE, _RENDER_SCALE), clip=clip_rect, alpha=False)
    annotation = _call_vision(pix.tobytes("png"))
    if not annotation:
        return []
    offset_x = clip_rect.x0 if clip_rect is not None else 0.0
    offset_y = clip_rect.y0 if clip_rect is not None else 0.0
    scale = 1.0 / _RENDER_SCALE
    pixels = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    return _text_boxes_from_annotation(
        annotation, id_prefix, scale, scale, offset_x, offset_y, pixels=pixels
    )


def ocr_page(fitz_page, page_index):
    """Renders one WHOLE page to an image, OCRs it, returns extract.py-
    shaped page JSON (every text_box carries `"source": "ocr"`). For a
    fully flattened/rasterized page with no real vector text anywhere."""
    text_boxes = _ocr_region(fitz_page, None, f"p{page_index}-ocr-l")
    return {
        "page_number": page_index + 1,
        "page_index": page_index,
        "width": fitz_page.rect.width,
        "height": fitz_page.rect.height,
        "text_boxes": text_boxes,
    }


def image_pixels(fitz_doc, xref):
    """An embedded image as an HxWx3 uint8 RGB array (alpha dropped, any
    colourspace converted) -- shared with reinsert.py's inpainting."""
    pix = pymupdf.Pixmap(fitz_doc, xref)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3].copy()


def _runs(mask, min_len):
    """(row, start, end) of horizontal True-runs at least min_len long."""
    padded = np.pad(mask, ((0, 0), (1, 1))).astype(np.int8)
    d = np.diff(padded, axis=1)
    rows_s, starts = np.nonzero(d == 1)
    _, ends = np.nonzero(d == -1)
    keep = ends - starts >= min_len
    return zip(rows_s[keep], starts[keep], ends[keep])


def raster_segments(pixels, word_boxes, min_frac=0.08):
    """Long straight lines drawn INSIDE an image (table rules, frames,
    dashed borders), as ([(x, y0, y1)], [(y, x0, x1)]) in image pixels.

    Numpy stand-in for the reference script's cv2 morphology: pixels that
    contrast with the image's dominant colour, dashes bridged across gaps
    up to 3px, kept if the run is long (>= min_frac of the image, >= 25px)
    and THIN (the pixels 4px either side are mostly background -- a
    filled area also has long runs, but isn't a line). Runs through the
    core of a word (underlines/strike-throughs of text) are dropped."""
    gray = pixels.astype(np.float32).mean(axis=2)
    dark = np.abs(gray - np.median(gray)) > 60
    out = []
    for vertical in (False, True):
        m = dark.T if vertical else dark
        closed = m.copy()
        for k in (1, 2, 3):
            closed[:, k:] |= m[:, :-k]
        n_rows = m.shape[0]
        min_len = max(25, int(m.shape[1] * min_frac))
        segs, seen = [], set()
        for r, a, b in _runs(closed, min_len):
            above = m[max(r - 4, 0), a:b].mean()
            below = m[min(r + 4, n_rows - 1), a:b].mean()
            if above > 0.5 or below > 0.5:
                continue
            key = (int(r) // 3, int(a) // 10, int(b) // 10)
            if key in seen:
                continue
            seen.add(key)
            crosses = False
            for x0, y0, x1, y1 in word_boxes:
                if vertical:
                    x0, y0, x1, y1 = y0, x0, y1, x1
                h = y1 - y0
                if y0 + 0.2 * h < r < y1 - 0.2 * h and a < x1 and b > x0:
                    crosses = True
                    break
            if not crosses:
                segs.append((float(r) + 0.5, float(a), float(b)))
        out.append(segs)
    hsegs, vsegs = out
    return vsegs, hsegs


def _ocr_xref(fitz_doc, xref, cache):
    """(annotation, width, height, pixels) for an embedded image, calling
    Vision at most ONCE per image per document: the same xref is often
    placed on several pages (a repeated logo, a reused diagram)."""
    if cache is not None and xref in cache:
        return cache[xref]
    image_dict = fitz_doc.extract_image(xref)
    pixels = image_pixels(fitz_doc, xref)
    if image_dict["ext"] in ("jpeg", "jpg", "png"):
        data = image_dict["image"]
    else:
        # JPX / JBIG2 / etc. aren't Vision input formats.
        data = pymupdf.Pixmap(pymupdf.csRGB, pixels.shape[1], pixels.shape[0], pixels.tobytes(), False).tobytes("png")
    result = (_call_vision(data), pixels.shape[1], pixels.shape[0], pixels)
    if cache is not None:
        cache[xref] = result
    return result


def _ocr_raw_image(fitz_doc, xref, bbox_rect, id_prefix, cache=None):
    """OCRs an embedded image's OWN raw pixel data (extracted directly
    from the PDF via its xref), never a rendered page region -- see
    ocr_image_regions's docstring for why that distinction is load-
    bearing. Vision's returned pixel coordinates are in the image's own
    NATIVE resolution, mapped back to page space via bbox_rect's width/
    height divided by that native width/height -- a per-axis scale,
    since an image's placement can stretch its two axes differently
    (confirmed non-uniform on a real seller PDF: PDF transform diagonal
    entries differed between axes for more than one embedded image)."""
    annotation, width, height, pixels = _ocr_xref(fitz_doc, xref, cache)
    if not annotation:
        return [], [], []
    scale_x = bbox_rect.width / width
    scale_y = bbox_rect.height / height
    ox, oy = bbox_rect.x0, bbox_rect.y0
    boxes = _text_boxes_from_annotation(
        annotation, id_prefix, scale_x, scale_y, ox, oy, xref=xref, pixels=pixels,
    )
    # Hard outer limit for the translated text's layout box (layout_units.py):
    # text baked into an image stays inside that image.
    for box in boxes:
        box["image_bbox"] = list(bbox_rect)
    if not boxes:
        return [], [], []
    seg_key = ("segments", xref)
    if cache is not None and seg_key in cache:
        vpx, hpx = cache[seg_key]
    else:
        vpx, hpx = raster_segments(pixels, [w for b in boxes for w in b["ocr"]["words_px"]])
        if cache is not None:
            cache[seg_key] = (vpx, hpx)
    vsegs = [[x * scale_x + ox, y0 * scale_y + oy, y1 * scale_y + oy] for x, y0, y1 in vpx]
    hsegs = [[y * scale_y + oy, x0 * scale_x + ox, x1 * scale_x + ox] for y, x0, x1 in hpx]
    return boxes, vsegs, hsegs


def ocr_image_regions(fitz_page, page_index, images, cache=None):
    """For each embedded image on an otherwise-normal page (one that DOES
    have real vector text elsewhere), OCRs just that image's OWN raw
    pixel data. Returns (text_boxes, vsegs, hsegs) in page space: the
    text found -- meant to be ADDED to that page's existing
    vector-extracted text_boxes, not replace them -- and the straight
    lines drawn inside those images (raster_segments), which layout
    treats as borders like drawn vector lines.

    `images`: `page.get_image_info(xrefs=True)` dicts. `cache`: a dict
    shared across a document's pages (see _ocr_xref).

    Deliberately OCRs the image's raw bytes (`_ocr_raw_image`), NOT a
    `get_pixmap(clip=...)` render of that region (`_ocr_region`, only
    correct for a genuinely empty/flattened whole page) -- found this
    the hard way on a real seller PDF: a page with real vector labels
    ("4335 mm", "Step 2. Foundation frame.", etc.) positioned on top of
    a diagram image. Rendering that image's clip region for OCR also
    rendered those real vector labels into the same pixels, so Vision
    re-detected text ALREADY correctly captured by normal vector
    extraction -- both got drawn, independently, at slightly different
    positions, producing visible doubled/ghosted text in the real
    output. OCR-ing the raw embedded image bytes directly is immune to
    this by construction: there is no vector content in a raw image
    file, only whatever's actually baked into its own pixels.

    Skips images below _MIN_IMAGE_OCR_AREA (not worth an API call), and
    rotated/mirrored placements (the bbox mapping assumes an upright
    image). Also skips an image mostly overlapped by images already
    processed (see _MAX_IMAGE_OVERLAP_RATIO) as a secondary safeguard
    against genuinely duplicated raster content (e.g. a zoomed-detail
    crop of another image) -- processes in PyMuPDF's
    page.get_image_info() order, so the FIRST image to claim a region
    wins and later ones judged redundant against it are skipped.
    """
    all_boxes, all_v, all_h = [], [], []
    claimed_rects = []
    for image_no, info in enumerate(images):
        xref = info.get("xref", 0)
        if xref <= 0:
            continue
        m = pymupdf.Matrix(info["transform"])
        if abs(m.b) > 1e-3 or abs(m.c) > 1e-3 or m.a <= 0 or m.d <= 0:
            print(f"ocr_vision: image {xref} on page {page_index} is rotated/mirrored, OCR skipped",
                  file=sys.stderr)
            continue
        rect = pymupdf.Rect(info["bbox"])
        area = rect.width * rect.height
        if area < _MIN_IMAGE_OCR_AREA:
            continue
        overlap_area = sum((rect & claimed).get_area() for claimed in claimed_rects)
        if overlap_area / area >= _MAX_IMAGE_OVERLAP_RATIO:
            continue
        claimed_rects.append(rect)
        boxes, vsegs, hsegs = _ocr_raw_image(
            fitz_page.parent, xref, rect, f"p{page_index}-ocr-img{image_no}-l", cache
        )
        all_boxes += boxes
        all_v += vsegs
        all_h += hsegs
    return all_boxes, all_v, all_h

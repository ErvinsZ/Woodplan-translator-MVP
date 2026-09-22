"""Redaction, re-layout and export -- the final pipeline stage.

Consumes classify_translate.py's output (every unit's
`pipeline.translated_paragraphs` filled in) plus the original PDF bytes.
Works per translation UNIT (layout_units.py), not per span, following
`working python script/pdf_translate.py`, which rendered the real sample
PDFs without the layout bugs the old span-level renderer had:

  - Only units whose text actually changed are touched. Numbers, part
    codes, and anything left untranslated keep their ORIGINAL glyphs --
    never redacted, never redrawn, byte-identical to the source.
  - Original text is removed glyph-only: a redaction with NO fill
    (`fill=False`) on a rect inset from the line bbox, applied with
    images/line-art untouched and TEXT_REMOVE. Nothing is painted, so no
    white box can land on a diagram (the old `fill=(1,1,1)` bbox
    redaction clipped diagram ink wherever a caption touched it), and
    the inset keeps neighbouring lines' glyphs out of the rect.
    `images=0` stays mandatory (known bug #6: the default images=2 bakes
    a border onto the page wherever a redaction rect touches an image).
  - Layout box per unit: start from the unit's original rect and grow
    right/down until a REAL obstacle -- a drawn line segment (table
    rule, dashed frame), any other text line on the page, a box already
    placed, the page margin/image bounds, or actual ink found by
    rendering the page (`PixelProbe`, which is how diagrams are seen at
    all: extract.py has no diagram geometry, they're raster images).
  - `insert_htmlbox(rect, html, scale_low=...)` wraps first and shrinks
    the WHOLE unit uniformly only if it still doesn't fit, returning
    (spare, scale) -- so a heading/list/paragraph always renders at one
    consistent size, and a failed fit is detectable (spare < 0), unlike
    `insert_textbox`'s silent draw-nothing (known bug #1). Below
    MIN_SCALE it retries with no floor and flags `needs_review`.
  - Rotation: 0/90/180/270 via insert_htmlbox's `rotate`; a translated
    unit at any other angle is left in the original language and
    flagged (every diagonal span seen so far is a numeric angle callout,
    which never becomes a unit at all).

OCR-sourced units (text baked into an image) have no glyphs to redact.
Text in an embedded image is erased by inpainting that image's own
pixels (image_erase.py) and swapping it in with `replace_image` --
collected per image across the whole document, applied once per image.
A whole flattened page (no embedded image to edit) gets its words
covered with the locally sampled background colour instead of white.
"""

import html
import os
import sys

import numpy as np
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from image_erase import erase_words
from layout_units import page_lines
from ocr_vision import image_pixels

# Smallest uniform scale insert_htmlbox may use before a unit is flagged
# (CLAUDE.md: shrink to ~75-80% before flagging for human review).
MIN_SCALE = 0.75
# How far a unit may grow downward, as a multiple of its own height.
MAX_GROW = 1.0

# Extra margin around a whole-page OCR word's cover rect beyond Vision's
# ink-tight word box -- tuned against real Vision output (0.75pt and 3pt
# both left visible fragments of ascenders/descenders; 5pt cleared them).
_OCR_ERASE_PADDING = 5.0
# JPEG quality for an inpainted image that was a JPEG originally.
_JPEG_QUALITY = 90


def _y_overlap(a, b, tol=1.0):
    return a.y0 < b.y1 - tol and a.y1 > b.y0 + tol


def _x_overlap(ax0, ax1, bx0, bx1, tol=1.0):
    return ax0 < bx1 - tol and ax1 > bx0 + tol


def _right_limit(rect, bounds, vsegs, obstacles, pad):
    right = bounds.x1
    for x, y0, y1 in vsegs:
        if x >= rect.x1 - 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            right = min(right, x - pad)
    for o in obstacles:
        if o.x0 >= rect.x1 - 1 and _y_overlap(o, rect):
            right = min(right, o.x0 - pad)
    return max(right, rect.x1)


def _left_limit(rect, bounds, vsegs, obstacles, pad):
    left = bounds.x0
    for x, y0, y1 in vsegs:
        if x <= rect.x0 + 0.5 and y0 < rect.y1 - 1 and y1 > rect.y0 + 1:
            left = max(left, x + pad)
    for o in obstacles:
        if o.x1 <= rect.x0 + 1 and _y_overlap(o, rect):
            left = max(left, o.x1 + pad)
    return min(left, rect.x0)


class PixelProbe:
    """Low-res render of the page (1px = 1pt) used to see where the page
    is really empty -- the only way this pipeline "sees" raster diagrams.
    Only applied to text on a clean background; over photos/renders the
    geometric limits alone are used (the original text overlapped the
    picture there anyway)."""

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

    def ring_color(self, R, pad=3.0):
        """Median colour (0-1 RGB) just outside R -- what to cover R with."""
        X0, Y0, X1, Y1 = self._box(R.x0 - pad, R.y0 - pad, R.x1 + pad, R.y1 + pad)
        reg = self.a[Y0:Y1, X0:X1]
        if reg.size == 0:
            return (1, 1, 1)
        ring = np.concatenate([reg[0], reg[-1], reg[:, 0], reg[:, -1]])
        return tuple(float(v) / 255 for v in np.median(ring, axis=0))

    def _ink(self, reg, bg):
        return np.abs(reg - bg).max(axis=2) > self.tol

    def free_right(self, R, limit, bg):
        x0, y0, x1, y1 = self._box(R.x1 + 1, R.y0 + 0.15 * R.height, limit, R.y1 - 0.15 * R.height)
        if x1 <= x0 or y1 <= y0:
            return limit
        cols = self._ink(self.a[y0:y1, x0:x1], bg).mean(axis=0)
        hit = np.nonzero(cols > 0.02)[0]
        return limit if len(hit) == 0 else (x0 + hit[0]) / self.sx

    def free_left(self, R, limit, bg):
        x0, y0, x1, y1 = self._box(limit, R.y0 + 0.15 * R.height, R.x0 - 1, R.y1 - 0.15 * R.height)
        if x1 <= x0 or y1 <= y0:
            return limit
        cols = self._ink(self.a[y0:y1, x0:x1], bg).mean(axis=0)
        hit = np.nonzero(cols > 0.02)[0]
        return limit if len(hit) == 0 else (x0 + hit[-1] + 1) / self.sx

    def free_down(self, x0_, x1_, top, limit, bg):
        x0, y0, x1, y1 = self._box(x0_, top + 1, x1_, limit)
        if x1 <= x0 or y1 <= y0:
            return limit
        rows = self._ink(self.a[y0:y1, x0:x1], bg).mean(axis=1)
        hit = np.nonzero(rows > 0.01)[0]
        return limit if len(hit) == 0 else (y0 + hit[0]) / self.sy


def layout_rect(unit, obstacles, vsegs, hsegs, probe):
    """(rect, align) the translated unit may occupy -- see module docstring."""
    R = pymupdf.Rect(unit["rect"])
    if unit["rotate"] != 0:
        return R, "left"
    size = unit["size"]
    pad = max(1.0, 0.25 * size)
    b = pymupdf.Rect(unit["bounds"])
    right = _right_limit(R, b, vsegs, obstacles, pad)
    left = _left_limit(R, b, vsegs, obstacles, pad)
    bg = probe.background(R, size)
    if bg is not None:
        right = max(R.x1, min(right, probe.free_right(R, right + pad, bg) - pad))

    align, x0, x1 = "left", R.x0, right
    line_rects = [pymupdf.Rect(l["rect"]) for l in unit["lines"]]
    lgap, rgap = R.x0 - left, right - R.x1
    centers = [(r.x0 + r.x1) / 2 for r in line_rects]
    lefts = [r.x0 for r in line_rects]
    centred_lines = len(line_rects) == 1 or (max(centers) - min(centers) < 0.5 * size
                                             and max(lefts) - min(lefts) > size)
    if centred_lines and lgap > 2 * size and abs(lgap - rgap) < max(3.0, 0.05 * (right - left)):
        cx = (R.x0 + R.x1) / 2
        half = min(cx - left, right - cx)
        align, x0, x1 = "center", cx - half, cx + half
    elif len(line_rects) == 1 and rgap < 0.5 * size and lgap > 2 * size:
        # A label pinned against something on its right -- typically a
        # callout "STEP 6 ●──" ending at its leader dot. Growing right is
        # impossible and growing down wraps it; grow LEFT instead, right-
        # aligned, so it stays attached to its leader.
        if bg is not None:
            left = min(R.x0, max(left, probe.free_left(R, left - pad, bg) + pad))
        if R.x0 - left > 2 * size:
            align, x0, x1 = "right", left, right

    line_height = max(unit["line_height"], 1.2)
    max_bottom = min(b.y1, R.y1 + MAX_GROW * max(R.height, size * 1.3))
    if len(line_rects) == 1 and any(
        _y_overlap(o, R) and b.x0 <= (o.x0 + o.x1) / 2 <= b.x1 for o in obstacles
    ):
        # A single line with text beside it on the same row is a table
        # cell / row label: wrapping down would run into the next row, so
        # it may only shrink (found on a real OCR'd materials table:
        # "Cost per Unit" -> "Coste por unidad" wrapped into row 1).
        max_bottom = R.y1
    hard = b.y1  # first real obstacle below (no padding)
    for y, sx0, sx1 in hsegs:
        if y >= R.y1 - 0.5 and _x_overlap(sx0, sx1, x0, x1):
            hard = min(hard, y)
    for o in obstacles:
        if o.y0 >= R.y1 - 1 and _x_overlap(o.x0, o.x1, x0, x1):
            hard = min(hard, o.y0)
    if bg is not None:
        hard = min(hard, probe.free_down(x0, x1, R.y1, hard, bg))
    bottom = max(R.y1, min(max_bottom, hard - pad))
    # A PDF line bbox is tighter than size * line-height: always allow room
    # for the ORIGINAL number of lines at 100% size -- with the first
    # baseline aligned to the original one (see `top`), the glyphs occupy
    # the same place the original glyphs did.
    need = R.y0 + len(line_rects) * size * line_height * 1.02 + 0.5
    bottom = max(bottom, min(need, hard + 0.25 * size))
    # CSS line-height puts half the extra leading above the first line;
    # shift up by that so the first baseline lands where the original was.
    top = R.y0 - (line_height - 1) / 2 * size
    if unit["source"] == "ocr":
        # An OCR box starts at the INK top (cap or x-height), not at a font
        # ascender line like a vector bbox, so anchor on the real baseline
        # instead: insert_htmlbox puts the first baseline at
        # top + (0.9 + (line_height - 1) / 2) * size (measured).
        top = unit["lines"][0]["baseline"] - (0.9 + (line_height - 1) / 2) * size
        # Room for the original line count at full size from that top --
        # the part below the last baseline is invisible leading, so it may
        # overlap the next row's box without any glyph overlapping.
        bottom = max(bottom, top + len(line_rects) * size * line_height * 1.02 + 0.5)
    return pymupdf.Rect(x0, top, x1, bottom), align


def _css(unit, align):
    c = unit["color"]
    color = "#%02x%02x%02x" % ((c >> 16) & 255, (c >> 8) & 255, c & 255)
    return (
        "* {margin:0; padding:0;} p {margin:0;} "
        f"body {{font-family: {unit['family']}; font-size: {unit['size']:.2f}px;"
        f" line-height: {unit['line_height']:.2f}; color: {color}; text-align: {align};"
        f" font-weight: {'bold' if unit['bold'] else 'normal'};"
        f" font-style: {'italic' if unit['italic'] else 'normal'};}}"
    )


def _paragraph_html(text, lead):
    """<p> for one paragraph; a styled lead-in code (layout_units.py's
    "lead", e.g. a bold "D1-A.") keeps its own weight/style if the
    translation still starts with it."""
    if lead and text.startswith(lead["text"]):
        style = (
            f"font-weight: {'bold' if lead['bold'] else 'normal'};"
            f" font-style: {'italic' if lead['italic'] else 'normal'};"
        )
        rest = text[len(lead["text"]):]
        return f'<p><span style="{style}">{html.escape(lead["text"])}</span>{html.escape(rest)}</p>'
    return f"<p>{html.escape(text)}</p>"


def _changed(unit):
    translated = unit["pipeline"].get("translated_paragraphs")
    if not translated:
        return False
    return any(t is not None and t != p["text"] for t, p in zip(translated, unit["paragraphs"]))


def _redact_rect(line_rect, rotate):
    r = pymupdf.Rect(line_rect)
    if rotate in (90, 270):
        inset = 0.2 * r.width
        return pymupdf.Rect(r.x0 + inset, r.y0 + 0.3, r.x1 - inset, r.y1 - 0.3)
    inset = 0.2 * r.height
    return pymupdf.Rect(r.x0 + 0.3, r.y0 + inset, r.x1 - 0.3, r.y1 - inset)


def reinsert_page(fitz_page, page_json, image_erasures=None):
    """Redacts and re-lays-out every changed unit on one page, in place.

    `image_erasures` (xref -> (page_index, [word boxes in image px])) is
    filled with the OCR words to inpaint; reinsert_and_export applies it
    once per image after all pages are done. Without it (standalone
    single-page use) the page's images are inpainted immediately."""
    apply_now = image_erasures is None
    if apply_now:
        image_erasures = {}
    units = page_json.get("units", [])
    to_render = []
    for unit in units:
        if not _changed(unit):
            continue
        if unit["rotate"] is None:
            unit["pipeline"]["needs_review"] = True
            print(f"reinsert: {unit['id']} at unsupported angle, left untranslated", file=sys.stderr)
            continue
        to_render.append(unit)
    if not to_render:
        return page_json

    probe = PixelProbe(fitz_page)
    all_lines = page_lines(page_json)
    segments = page_json.get("segments", {"v": [], "h": []})
    vsegs, hsegs = segments["v"], segments["h"]

    redacted = False
    for unit in to_render:
        for line in unit["lines"]:
            if line["source"] == "ocr":
                continue
            fitz_page.add_redact_annot(
                _redact_rect(line["rect"], unit["rotate"]), fill=False, cross_out=False
            )
            redacted = True
    if redacted:
        fitz_page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    for unit in to_render:
        for line in unit["lines"]:
            if line["source"] != "ocr":
                continue
            ocr = line["ocr"]
            if ocr.get("xref"):
                entry = image_erasures.setdefault(ocr["xref"], (page_json["page_index"], []))
                entry[1].extend(ocr["words_px"])
            else:
                pad = _OCR_ERASE_PADDING
                for word in ocr["words_pt"]:
                    r = pymupdf.Rect(word) + (-pad, -pad, pad, pad)
                    fitz_page.draw_rect(r, fill=probe.ring_color(r), color=None)

    placed = []
    for unit in sorted(to_render, key=lambda u: (round(u["rect"][1]), u["rect"][0])):
        own = {l["key"] for l in unit["lines"]}
        obstacles = [pymupdf.Rect(l["rect"]) for l in all_lines if l["key"] not in own] + placed
        rect, align = layout_rect(unit, obstacles, vsegs, hsegs, probe)
        body = "".join(
            _paragraph_html(t if t is not None else p["text"], p.get("lead"))
            for t, p in zip(unit["pipeline"]["translated_paragraphs"], unit["paragraphs"])
        )
        css = _css(unit, align)
        rot = unit["rotate"]
        spare, scale = fitz_page.insert_htmlbox(rect, body, css=css, scale_low=MIN_SCALE, rotate=rot)
        overflow = spare < 0
        if overflow:
            spare, scale = fitz_page.insert_htmlbox(rect, body, css=css, scale_low=0, rotate=rot)
            unit["pipeline"]["needs_review"] = True
        unit["pipeline"]["scale"] = round(scale, 3)
        unit["pipeline"]["overflow"] = overflow
        if rot in (0, 180):
            placed.append(pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1 - max(0.0, spare)))
        else:
            placed.append(rect)
    if apply_now:
        apply_image_erasures(fitz_page.parent, image_erasures)
    return page_json


def apply_image_erasures(doc, image_erasures):
    """Inpaints each image's OCR'd words and swaps the edited image in.
    A JPEG stays a JPEG (keeps the file size in check); an image with a
    soft mask keeps its alpha."""
    for xref, (page_index, words) in image_erasures.items():
        try:
            original = pymupdf.Pixmap(doc, xref)
            erased = erase_words(image_pixels(doc, xref), words)
            h, w = erased.shape[:2]
            pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, erased.tobytes(), False)
            page = doc[page_index]
            if original.alpha:
                alpha = np.frombuffer(original.samples, np.uint8).reshape(
                    original.h, original.w, original.n)[..., -1]
                pix = pymupdf.Pixmap(pix, 1)
                pix.set_alpha(alpha.tobytes())
                page.replace_image(xref, pixmap=pix)
            elif doc.extract_image(xref)["ext"] in ("jpeg", "jpg"):
                page.replace_image(xref, stream=pix.tobytes("jpeg", jpg_quality=_JPEG_QUALITY))
            else:
                page.replace_image(xref, pixmap=pix)
        except Exception as exc:
            print(f"reinsert: could not inpaint image {xref} ({exc!r})", file=sys.stderr)


def reinsert_and_export(pdf_bytes, document_json, page_indices=None):
    """Runs redaction/re-layout across pages, returns (pdf_bytes, document_json).

    page_indices restricts which pages get modified (e.g. for testing a
    single page); omit to process every page in document_json.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        image_erasures = {}
        for page_json in document_json["pages"]:
            if page_indices is not None and page_json["page_index"] not in page_indices:
                continue
            reinsert_page(doc[page_json["page_index"]], page_json, image_erasures)
        apply_image_erasures(doc, image_erasures)

        output_bytes = doc.tobytes(garbage=4, deflate=True)
    finally:
        doc.close()

    return output_bytes, document_json

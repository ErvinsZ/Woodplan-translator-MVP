"""Refit, redaction/reinsertion, and export — the final pipeline stages.

Consumes classify_translate.py's output (every text box already has
`pipeline.translated_text` set) plus the original PDF bytes, and produces
a translated PDF: shrink font where the translation overflows its
original footprint, wipe the original text with a redaction, draw the
translated (or unchanged) text back at the exact original position, and
re-save. Never touches the underlying diagram images.

Known-bug fixes this module exists to encode (see CLAUDE.md):
  - Reinsertion uses `page.insert_text(origin, ...)`, one call per span at
    its exact baseline origin — never `insert_textbox` into the bbox,
    which silently draws nothing for tight single-line boxes.
  - Rotation uses the per-span `rotation_angle` degrees captured by
    extract.py, not a re-derived horizontal/vertical guess.
  - `insert_text(rotate=...)` only accepts 0/90/180/270 (verified against
    the installed PyMuPDF's own docstring) but real plans have true
    diagonal callouts (e.g. angle-dimension labels). Every non-zero angle
    here goes through `morph=(Point(origin), Matrix(1,1).prerotate(angle))`
    instead of `rotate=`, which empirically round-trips to the exact same
    `rotation_angle` extract.py reads back out (verified for 0, ±45, ±90,
    and non-axis angles) — one code path handles every angle correctly,
    including the axis-aligned cases the old `rotate=` int also covered.
"""

import base64
import json
from http.server import BaseHTTPRequestHandler

import pymupdf

# Below this, an angle is treated as exactly axis-aligned (0/±90) when
# picking the bbox dimension to measure available space against.
_ROTATION_EPSILON_DEGREES = 0.5

# CLAUDE.md: "Shrink font down to a floor (~75-80% of original) before
# flagging for human review."
_FONT_SHRINK_STEP = 0.3
_FONT_SHRINK_FLOOR_RATIO = 0.75


def _text_width(text, font_size):
    return pymupdf.get_text_length(text, fontname="helv", fontsize=font_size)


def _available_length(text_box):
    """How much room the original span had along its writing direction.

    Axis-aligned text: bbox width/height, exactly as pipeline_tested.py
    validated against real data.

    Diagonal text: the axis-aligned bbox doesn't decompose into a single
    along-direction length without also knowing font ascent/descent, so
    this falls back to the original text's own rendered width as an
    "it fit before" reference. Every diagonal span found so far in
    sample_plan.pdf is a numeric angle-callout (classification "code"),
    never translated, so this approximation isn't exercised by real
    content yet — flagged here rather than silently assumed correct.
    """
    bbox = text_box["bbox"]
    angle = text_box["rotation_angle"]

    if abs(angle) < _ROTATION_EPSILON_DEGREES:
        return bbox[2] - bbox[0]
    if abs(abs(angle) - 90) < _ROTATION_EPSILON_DEGREES:
        return bbox[3] - bbox[1]
    return _text_width(text_box["text"], text_box["font"]["size"])


def refit_span(text_box):
    """Fills in pipeline.overflow / used_font_size / available_len.

    Escalates needs_review to True on overflow, but never clears a
    needs_review already set by classify_translate.py (e.g. an unmatched
    span is still worth a human look even if it happens to fit).
    """
    pipeline = text_box["pipeline"]
    translated = pipeline["translated_text"]
    font_size = text_box["font"]["size"]
    available_len = _available_length(text_box)

    used_size = font_size
    fits = _text_width(translated, used_size) <= available_len
    if not fits:
        floor = font_size * _FONT_SHRINK_FLOOR_RATIO
        shrink = font_size
        while shrink > floor:
            shrink -= _FONT_SHRINK_STEP
            if _text_width(translated, shrink) <= available_len:
                used_size = shrink
                fits = True
                break

    pipeline["used_font_size"] = round(used_size, 2)
    pipeline["overflow"] = not fits
    pipeline["available_len"] = round(available_len, 2)
    if not fits:
        pipeline["needs_review"] = True

    return text_box


def _unpack_color(color_int):
    r = ((color_int >> 16) & 255) / 255
    g = ((color_int >> 8) & 255) / 255
    b = (color_int & 255) / 255
    return (r, g, b)


def _insert_span(page, text_box):
    translated = text_box["pipeline"]["translated_text"]
    if not translated.strip():
        return

    origin = text_box["origin"]
    used_size = text_box["pipeline"]["used_font_size"]
    angle = text_box["rotation_angle"]
    color = _unpack_color(text_box["font"]["color"])

    if abs(angle) < _ROTATION_EPSILON_DEGREES:
        page.insert_text(origin, translated, fontsize=used_size, fontname="helv", color=color)
    else:
        point = pymupdf.Point(origin)
        matrix = pymupdf.Matrix(1, 1).prerotate(angle)
        page.insert_text(
            origin, translated, fontsize=used_size, fontname="helv", color=color,
            morph=(point, matrix),
        )


def reinsert_page(fitz_page, page_json):
    """Refits, redacts, and reinserts every text box on one page.

    Mutates fitz_page in place; page_json's text_boxes must already carry
    pipeline.translated_text (i.e. classify_translate.py has run).
    """
    for text_box in page_json["text_boxes"]:
        refit_span(text_box)

    # Redact every span, not just translated ones -- a code/number span
    # still needs its original glyphs wiped before the identical text is
    # redrawn at the (possibly refit) size, so the whole page uses one
    # consistent rendering pass rather than mixing original and new text.
    for text_box in page_json["text_boxes"]:
        fitz_page.add_redact_annot(pymupdf.Rect(text_box["bbox"]), fill=(1, 1, 1))
    fitz_page.apply_redactions()

    for text_box in page_json["text_boxes"]:
        _insert_span(fitz_page, text_box)

    return page_json


def reinsert_and_export(pdf_bytes, document_json, page_indices=None):
    """Runs refit/redact/reinsert across pages, returns (pdf_bytes, document_json).

    page_indices restricts which pages get modified (e.g. for testing a
    single page); omit to process every page in document_json.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_json in document_json["pages"]:
            if page_indices is not None and page_json["page_index"] not in page_indices:
                continue
            reinsert_page(doc[page_json["page_index"]], page_json)

        output_bytes = doc.tobytes(garbage=4, deflate=True)
    finally:
        doc.close()

    return output_bytes, document_json


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point.

    POST JSON: {"pdf_base64": "...", "document": <classify_translate.py
    output>, "page_index": optional int}. Returns JSON:
    {"pdf_base64": <translated pdf>, "document": <document with overflow/
    used_font_size/available_len/needs_review filled in>}.
    """

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)

        pdf_bytes = base64.b64decode(payload["pdf_base64"])
        document = payload["document"]
        page_index = payload.get("page_index")
        page_indices = {page_index} if page_index is not None else None

        output_bytes, updated_document = reinsert_and_export(pdf_bytes, document, page_indices)

        response = json.dumps({
            "document": updated_document,
            "pdf_base64": base64.b64encode(output_bytes).decode("ascii"),
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(response)

"""PDF text extraction — format-specific stage of the localization pipeline.

Pulls every real vector text span out of a page via PyMuPDF's structured
`get_text('dict')` output and emits it as plain JSON: one entry per span,
carrying exactly the geometry the later classify/translate, refit, and
reinsertion stages need. No translation or classification happens here.

Known-bug fixes this module exists to encode (see CLAUDE.md):
  - Writing direction lives on the LINE, not the span. `span.get('dir')`
    is always None; must read `line['dir']` and propagate it to every span
    in that line, or rotated/vertical labels get silently misclassified.
  - Downstream reinsertion must use the span's exact baseline `origin`
    point (`page.insert_text`), not `insert_textbox` into the bbox — so
    origin is captured here as first-class, not derived later from bbox.
  - `rotation_angle` is a continuous float, not a horizontal/vertical
    binary. Real plans have true diagonal callouts (e.g. angle-dimension
    labels like "64 deg" following the diagram's actual cut angle) —
    found by running this extractor against sample_plan.pdf, where ~2%
    of spans have a line direction that's neither axis. Flattening those
    to 0 degrees would silently misplace them on reinsertion, which for
    any non-multiple-of-90 angle needs PyMuPDF's `morph=` matrix rather
    than the `rotate=` int (that param only accepts 0/90/180/270).
"""

import base64
import json
import math
from http.server import BaseHTTPRequestHandler

import pymupdf

# Below this, a line direction is treated as exactly horizontal/vertical —
# absorbs float noise like dir=(1.0, -9.999999974752427e-07) from PyMuPDF.
_ROTATION_EPSILON_DEGREES = 0.5


def _rotation_angle(line_dir):
    """True writing-direction angle in degrees (CCW positive, 0 = horizontal).

    Real plans contain diagonal callouts (e.g. "64°", "26°" angle labels
    following a diagram's actual cut angle), not just horizontal/vertical
    text. A binary horizontal-vs-vertical model silently flattens those to
    0 degrees and loses the data needed to place them correctly on
    reinsertion — which itself must use a rotation matrix (`morph=`) for
    any non-multiple-of-90 angle, since `insert_text(rotate=...)` only
    accepts 0/90/180/270.
    """
    dx, dy = line_dir
    angle = math.degrees(math.atan2(-dy, dx))
    if abs(angle) < _ROTATION_EPSILON_DEGREES:
        return 0.0
    if abs(abs(angle) - 90) < _ROTATION_EPSILON_DEGREES:
        return 90.0 if angle > 0 else -90.0
    return round(angle, 3)


def extract_page(page, page_index):
    """Extract every non-blank text span on one page into the JSON schema."""
    page_dict = page.get_text("dict")
    text_boxes = []

    for block_no, block in enumerate(page_dict["blocks"]):
        if "lines" not in block:
            continue  # image/drawing block, not text

        for line_no, line in enumerate(block["lines"]):
            # Direction lives on the line, not the span (known bug #2).
            line_dir = line.get("dir", (1, 0))
            rotation_angle = _rotation_angle(line_dir)
            is_rotated = rotation_angle != 0.0

            for span_no, span in enumerate(line["spans"]):
                if not span["text"].strip():
                    continue

                text_boxes.append({
                    "id": f"p{page_index}-b{block_no}-l{line_no}-s{span_no}",
                    # Raw, unstripped text — whitespace is preserved
                    # deliberately, some spans carry meaningful leading/
                    # trailing whitespace used for layout (known bug #3).
                    "text": span["text"],
                    "bbox": list(span["bbox"]),
                    "origin": list(span["origin"]),
                    "font": {
                        "name": span["font"],
                        "size": span["size"],
                        "color": span["color"],
                    },
                    "line_dir": list(line_dir),
                    "is_rotated": is_rotated,
                    "rotation_angle": rotation_angle,
                    # Filled in by classify_translate.py; kept here so the
                    # shape is stable end-to-end (mirrors the text_boxes
                    # JSONB column: extraction owns the structural fields,
                    # pipeline owns this nested blob).
                    "pipeline": {
                        "classification": None,
                        "target_lang": None,
                        "translated_text": None,
                        "needs_review": False,
                        "overflow": None,
                        "used_font_size": None,
                    },
                })

    return {
        "page_number": page_index + 1,
        "page_index": page_index,
        "width": page.rect.width,
        "height": page.rect.height,
        "text_boxes": text_boxes,
    }


def extract_document(pdf_stream_or_path, document_name=None):
    """Extract all pages of a PDF into the JSON schema."""
    doc = pymupdf.open(pdf_stream_or_path)
    try:
        pages = [extract_page(doc[i], i) for i in range(doc.page_count)]
    finally:
        doc.close()

    return {
        "document": document_name or "unknown.pdf",
        "page_count": len(pages),
        "pages": pages,
    }


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point.

    POST either raw PDF bytes (Content-Type: application/pdf) or JSON
    {"pdf_base64": "...", "document": "name.pdf", "page_index": 0}.
    Omit page_index to extract every page.
    """

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        document_name = None
        page_index = None

        if content_type.startswith("application/json"):
            payload = json.loads(body)
            pdf_bytes = base64.b64decode(payload["pdf_base64"])
            document_name = payload.get("document")
            page_index = payload.get("page_index")
        else:
            pdf_bytes = body

        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            if page_index is not None:
                result = extract_page(doc[page_index], page_index)
                result["document"] = document_name or "unknown.pdf"
            else:
                pages = [extract_page(doc[i], i) for i in range(doc.page_count)]
                result = {
                    "document": document_name or "unknown.pdf",
                    "page_count": len(pages),
                    "pages": pages,
                }
        finally:
            doc.close()

        response = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)

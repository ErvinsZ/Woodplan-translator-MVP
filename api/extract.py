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
import os
import sys
from http.server import BaseHTTPRequestHandler

import pymupdf

# Vercel's Python runtime loads the entrypoint via importlib (exec_module),
# not as a directly-run script, so this file's own directory is never
# auto-added to sys.path the way `python extract.py` would -- sibling
# imports (geometry.py, ocr_vision.py below) silently 404 with
# ModuleNotFoundError without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geometry import rotation_angle as _rotation_angle
from ocr_vision import ocr_page, ocr_image_regions


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
                    # "vector" (real PyMuPDF text) vs "ocr" (see
                    # ocr_vision.py, used by extract_document below for a
                    # flattened/rasterized page with no real text at all).
                    # reinsert.py branches on this: an OCR span has no
                    # underlying vector text to redact, so erasing the
                    # original means painting over image pixels directly
                    # instead of the normal redaction-annotation path.
                    "source": "vector",
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
    """Extract all pages of a PDF into the JSON schema.

    Two distinct OCR cases, both handled here (see ocr_vision.py's own
    module docstring for the full reasoning):

    1. A page with zero real vector text boxes is a flattened/rasterized
       page (a scan, or a plan exported to one page-covering image)
       rather than a page that's legitimately just sparse -- every real
       page found in this project's sample data carries at minimum a
       repeated copyright footer as real vector text, so genuine
       zero-text pages aren't expected outside this case. OCRs the WHOLE
       page in that case.
    2. A page that DOES have real vector text can still separately
       contain one or more embedded raster images with their OWN baked-in
       text (found on a real seller PDF -- a diagram image with real
       dimension labels in its pixels that plain vector extraction never
       touches at all, since the page as a whole isn't "empty"). Each
       embedded image above a minimum size gets OCR'd individually and
       any text found is ADDED to that page's normal vector text_boxes.

    Either way, an OCR failure (no GOOGLE_VISION_API_KEY configured,
    Vision API error) is caught per-page and just leaves that page (or
    that one image) without the extra text, same as if this fallback
    didn't exist -- it only ever adds coverage, never removes it.
    """
    if isinstance(pdf_stream_or_path, (bytes, bytearray)):
        doc = pymupdf.open(stream=pdf_stream_or_path, filetype="pdf")
    else:
        doc = pymupdf.open(pdf_stream_or_path)
    try:
        pages = []
        for i in range(doc.page_count):
            page = extract_page(doc[i], i)
            if not page["text_boxes"]:
                try:
                    page = ocr_page(doc[i], i)
                except Exception as exc:
                    print(
                        f"extract_document: OCR fallback failed on page {i} ({exc!r}), "
                        "leaving it with zero text boxes",
                        file=sys.stderr,
                    )
            else:
                images = [
                    (info["bbox"], info["xref"])
                    for info in doc[i].get_image_info(xrefs=True)
                ]
                if images:
                    try:
                        page["text_boxes"].extend(
                            ocr_image_regions(doc[i], i, images)
                        )
                    except Exception as exc:
                        print(
                            f"extract_document: per-image OCR failed on page {i} "
                            f"({exc!r}), leaving its embedded images untouched",
                            file=sys.stderr,
                        )
            pages.append(page)
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

        if page_index is not None:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                result = extract_page(doc[page_index], page_index)
                if not result["text_boxes"]:
                    try:
                        result = ocr_page(doc[page_index], page_index)
                    except Exception as exc:
                        print(
                            f"extract handler: OCR fallback failed on page "
                            f"{page_index} ({exc!r}), leaving it with zero text boxes",
                            file=sys.stderr,
                        )
                else:
                    images = [
                        (info["bbox"], info["xref"])
                        for info in doc[page_index].get_image_info(xrefs=True)
                    ]
                    if images:
                        try:
                            result["text_boxes"].extend(
                                ocr_image_regions(doc[page_index], page_index, images)
                            )
                        except Exception as exc:
                            print(
                                f"extract handler: per-image OCR failed on page "
                                f"{page_index} ({exc!r}), leaving its embedded images untouched",
                                file=sys.stderr,
                            )
                result["document"] = document_name or "unknown.pdf"
            finally:
                doc.close()
        else:
            # extract_document already includes the same OCR fallback and
            # manages its own doc open/close -- no need to duplicate that
            # loop here.
            result = extract_document(pdf_bytes, document_name)

        response = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)

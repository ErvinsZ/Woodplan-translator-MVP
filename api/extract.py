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

import os
import sys

import pymupdf

# Vercel's Python runtime loads the entrypoint via importlib (exec_module),
# not as a directly-run script, so this file's own directory is never
# auto-added to sys.path the way `python extract.py` would -- sibling
# imports (geometry.py, ocr_vision.py below) silently 404 with
# ModuleNotFoundError without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geometry import rotation_angle as _rotation_angle
from layout_units import build_units, extract_segments
from ocr_vision import ocr_page, ocr_image_regions


def _invisible_rects(page):
    """Bboxes of text drawn invisibly (render mode 3, or fully transparent)
    -- e.g. the hidden OCR text layer of a scanned PDF. Translating that
    text and redrawing it visibly would paint a second copy over the scan."""
    rects = []
    try:
        for trace in page.get_texttrace():
            if trace.get("type") == 3 or trace.get("opacity", 1) == 0:
                rects.append(pymupdf.Rect(trace["bbox"]))
    except Exception:
        pass
    return rects


def _mostly_inside(rect, others):
    for other in others:
        inter = rect & other
        if not inter.is_empty and inter.get_area() > 0.8 * max(rect.get_area(), 1e-6):
            return True
    return False


def extract_page(page, page_index):
    """Extract every non-blank, visible text span on one page into the JSON schema."""
    page_dict = page.get_text("dict")
    invisible = _invisible_rects(page)
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
                if span.get("alpha", 255) == 0 or (
                    invisible and _mostly_inside(pymupdf.Rect(span["bbox"]), invisible)
                ):
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
                        # bit 4 = bold, bit 1 = italic (layout_units.py)
                        "flags": span["flags"],
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


# OCR lines below this Vision confidence, or shorter than this (pt), are
# dropped -- noise, not text worth translating.
_OCR_MIN_CONFIDENCE = 0.6
_OCR_MIN_HEIGHT_PT = 4.0
# An OCR line overlapping real vector text this much is the same text
# seen twice (vector labels drawn on top of an image), not new text.
_OCR_MAX_VECTOR_OVERLAP = 0.3


def _overlap_frac(a, b):
    """Intersection area / area of the smaller rect."""
    inter = a & b
    if inter.is_empty:
        return 0.0
    return inter.get_area() / max(min(a.get_area(), b.get_area()), 1e-6)


def _keep_ocr_box(box, vector_rects):
    r = pymupdf.Rect(box["bbox"])
    if r.height < _OCR_MIN_HEIGHT_PT or box["ocr"]["min_conf"] < _OCR_MIN_CONFIDENCE:
        return False
    return not any(_overlap_frac(r, v) > _OCR_MAX_VECTOR_OVERLAP for v in vector_rects)


def extract_page_full(doc, page_index, ocr_cache=None):
    """extract_page + the OCR fallbacks (see extract_document) + the
    layout data reinsert.py needs: drawn line segments and translation
    units (layout_units.py). `ocr_cache` (a dict shared across one
    document's pages) makes each embedded image cost one Vision call."""
    fitz_page = doc[page_index]
    page = extract_page(fitz_page, page_index)
    raster_v, raster_h = [], []
    if not page["text_boxes"]:
        try:
            page = ocr_page(fitz_page, page_index)
            page["text_boxes"] = [tb for tb in page["text_boxes"] if _keep_ocr_box(tb, [])]
        except Exception as exc:
            print(
                f"extract: OCR fallback failed on page {page_index} ({exc!r}), "
                "leaving it with zero text boxes",
                file=sys.stderr,
            )
    else:
        images = fitz_page.get_image_info(xrefs=True)
        if images:
            try:
                vector_rects = [pymupdf.Rect(tb["bbox"]) for tb in page["text_boxes"]]
                boxes, raster_v, raster_h = ocr_image_regions(fitz_page, page_index, images, ocr_cache)
                page["text_boxes"].extend(tb for tb in boxes if _keep_ocr_box(tb, vector_rects))
            except Exception as exc:
                print(
                    f"extract: per-image OCR failed on page {page_index} "
                    f"({exc!r}), leaving its embedded images untouched",
                    file=sys.stderr,
                )
    vsegs, hsegs = extract_segments(fitz_page)
    # + straight lines drawn inside OCR'd images (table rules, frames)
    vsegs += raster_v
    hsegs += raster_h
    page["segments"] = {"v": vsegs, "h": hsegs}
    page["units"] = build_units(page, vsegs)
    return page


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
        ocr_cache = {}
        pages = [extract_page_full(doc, i, ocr_cache) for i in range(doc.page_count)]
    finally:
        doc.close()

    return {
        "document": document_name or "unknown.pdf",
        "page_count": len(pages),
        "pages": pages,
    }

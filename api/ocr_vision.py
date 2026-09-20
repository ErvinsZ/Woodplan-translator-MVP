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
a service-account/OAuth2 flow -- same reasoning as gemini_translate.py's
and blob.py's existing hand-rolled-REST pattern: a service-account JWT flow
needs a crypto library to sign requests (PyJWT/cryptography, or
google-auth's own dependency chain), and `cryptography` ships a compiled
Rust extension -- exactly the class of dependency CLAUDE.md already warns
crashes real Vercel Python deployments (`google-genai`/`psycopg[binary]`
both did, confirmed via real `vercel dev` requests, not just a local venv).
A plain API key sidesteps that risk entirely and matches every other
external API call in this codebase.
"""

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import certifi
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
# these drive line reconstruction.
_LINE_END_BREAKS = {"LINE_BREAK", "EOL_SURE_SPACE"}
# A break after a word that should NOT get a space appended before the
# next word on the same line (a hyphenated wrap, or no break info at all).
_NO_SPACE_BREAKS = {"HYPHEN"}

# Word bounding-box height -> estimated font point size. Vision gives pixel
# geometry, not a font size -- this is a coarse cap-height-ish approximation
# (a real font's point size is somewhat taller than its rendered glyph
# bbox), not font metrics. Good enough for refit_span's shrink-to-fit
# comparison, which only needs a reasonable STARTING size, not an exact
# original (there is no original font here to match, unlike vector spans).
_HEIGHT_TO_FONT_SIZE_RATIO = 0.75

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


def _word_text_and_break(word):
    """Concatenates a word's symbols into text; returns (text, break_type)
    of whatever break follows its LAST symbol, or None if unspecified."""
    chars = []
    break_type = None
    for symbol in word.get("symbols", []):
        chars.append(symbol.get("text", ""))
        detected = symbol.get("property", {}).get("detectedBreak")
        break_type = detected["type"] if detected else None
    return "".join(chars), break_type


def _poly_vertices(bounding_poly):
    return bounding_poly.get("vertices") or bounding_poly.get("normalizedVertices") or []


def _lines_from_annotation(annotation):
    """Reconstructs one entry per VISUAL LINE from Vision's word/symbol
    hierarchy, matching extract.py's one-text-box-per-original-line
    granularity.

    Vision has no explicit "line" grouping -- only a break-type hint on
    each word's last symbol (SPACE/SURE_SPACE: more of this line follows;
    EOL_SURE_SPACE/LINE_BREAK: this line ends here; HYPHEN: next word
    continues with no space, a wrapped hyphenation). This walks each
    paragraph's words in order, building up line text directly (append
    the word, then conditionally append a separating space) rather than
    joining a word list afterward, so a hyphenated pair genuinely gets no
    space between them instead of needing to be un-joined after the fact.
    """
    lines = []
    for page in annotation.get("pages", []):
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                words = paragraph.get("words", [])
                line_text = ""
                line_vertices = []
                for i, word in enumerate(words):
                    text, break_type = _word_text_and_break(word)
                    if not text:
                        continue
                    line_text += text
                    line_vertices.extend(_poly_vertices(word.get("boundingBox", {})))

                    is_last_word = i == len(words) - 1
                    if is_last_word or break_type in _LINE_END_BREAKS:
                        lines.append((line_text, line_vertices))
                        line_text = ""
                        line_vertices = []
                    elif break_type not in _NO_SPACE_BREAKS:
                        line_text += " "
    return lines


def _line_to_text_box(box_id, text, vertices, scale_x, scale_y, offset_x=0.0, offset_y=0.0):
    """scale_x/scale_y: pixel-space -> page-point-space factors (1/_RENDER_SCALE,
    uniform, for a whole-page or page-region render; an embedded image's
    own bbox-width/native-pixel-width ratio, per axis, for a raw-image
    OCR -- see ocr_image_regions, where an image's placement can scale
    its two axes differently). offset_x/offset_y: page-space top-left of
    the region OCR'd (0,0 for a whole page; an embedded image's own bbox
    origin for a per-image OCR), added back in AFTER scaling, so the
    resulting bbox is always in PAGE space regardless of source."""
    xs = [v.get("x", 0) for v in vertices]
    ys = [v.get("y", 0) for v in vertices]
    x0 = min(xs) * scale_x + offset_x
    y0 = min(ys) * scale_y + offset_y
    x1 = max(xs) * scale_x + offset_x
    y1 = max(ys) * scale_y + offset_y
    height = max(y1 - y0, 1.0)
    font_size = round(height * _HEIGHT_TO_FONT_SIZE_RATIO, 2)

    # Angle of the top edge (first two vertices, Vision's documented
    # clockwise-from-top-left ordering) -- same convention geometry.py's
    # rotation_angle expects (top-down pixel space, negated dy). dx=0 is
    # the CORRECT value for genuinely vertical (90-degree) text, not a
    # degenerate case to guard against -- atan2 handles it natively, so
    # this must NOT fall back to a truthy-only default like `dx or 1`
    # (which would silently force dx=1 and corrupt exactly that case).
    if len(vertices) >= 2 and (
        vertices[1].get("x", 0) != vertices[0].get("x", 0)
        or vertices[1].get("y", 0) != vertices[0].get("y", 0)
    ):
        dx = vertices[1].get("x", 0) - vertices[0].get("x", 0)
        dy = vertices[1].get("y", 0) - vertices[0].get("y", 0)
        line_dir = (dx, dy)
    else:
        line_dir = (1, 0)
    rotation_angle = _rotation_angle(line_dir, _OCR_ROTATION_EPSILON_DEGREES)

    return {
        "id": box_id,
        "text": text,
        "bbox": [x0, y0, x1, y1],
        "origin": [x0, y1],
        "font": {"name": "", "size": font_size, "color": 0},
        "line_dir": list(line_dir),
        "is_rotated": rotation_angle != 0.0,
        "rotation_angle": rotation_angle,
        "source": "ocr",
        "pipeline": {
            "classification": None,
            "target_lang": None,
            "translated_text": None,
            "needs_review": False,
            "overflow": None,
            "used_font_size": None,
        },
    }


def _text_boxes_from_annotation(annotation, id_prefix, scale_x, scale_y, offset_x=0.0, offset_y=0.0):
    text_boxes = []
    for line_no, (text, vertices) in enumerate(_lines_from_annotation(annotation)):
        if not text.strip():
            continue
        text_boxes.append(
            _line_to_text_box(f"{id_prefix}{line_no}", text, vertices, scale_x, scale_y, offset_x, offset_y)
        )
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
    pix = fitz_page.get_pixmap(matrix=pymupdf.Matrix(_RENDER_SCALE, _RENDER_SCALE), clip=clip_rect)
    annotation = _call_vision(pix.tobytes("png"))
    if not annotation:
        return []
    offset_x = clip_rect.x0 if clip_rect is not None else 0.0
    offset_y = clip_rect.y0 if clip_rect is not None else 0.0
    scale = 1.0 / _RENDER_SCALE
    return _text_boxes_from_annotation(annotation, id_prefix, scale, scale, offset_x, offset_y)


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


def _ocr_raw_image(fitz_doc, xref, bbox_rect, id_prefix):
    """OCRs an embedded image's OWN raw pixel data (extracted directly
    from the PDF via its xref), never a rendered page region -- see
    ocr_image_regions's docstring for why that distinction is load-
    bearing. Vision's returned pixel coordinates are in the image's own
    NATIVE resolution, mapped back to page space via bbox_rect's width/
    height divided by that native width/height -- a per-axis scale,
    since an image's placement can stretch its two axes differently
    (confirmed non-uniform on a real seller PDF: PDF transform diagonal
    entries differed between axes for more than one embedded image)."""
    image_dict = fitz_doc.extract_image(xref)
    annotation = _call_vision(image_dict["image"])
    if not annotation:
        return []
    scale_x = bbox_rect.width / image_dict["width"]
    scale_y = bbox_rect.height / image_dict["height"]
    return _text_boxes_from_annotation(
        annotation, id_prefix, scale_x, scale_y, bbox_rect.x0, bbox_rect.y0
    )


def ocr_image_regions(fitz_page, page_index, images):
    """For each embedded image on an otherwise-normal page (one that DOES
    have real vector text elsewhere), OCRs just that image's OWN raw
    pixel data and returns any text found as a flat list of text_boxes
    in page-space coordinates -- meant to be ADDED to that page's
    existing vector-extracted text_boxes, not replace them.

    `images`: a list of (bbox, xref) pairs, e.g. from
    `[(info["bbox"], info["xref"]) for info in page.get_image_info(xrefs=True)]`.

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

    Skips images below _MIN_IMAGE_OCR_AREA (not worth an API call); an
    image with no text at all just costs one wasted call either way.
    Also skips an image mostly overlapped by images already processed
    (see _MAX_IMAGE_OVERLAP_RATIO) as a secondary safeguard against
    genuinely duplicated raster content (e.g. a zoomed-detail crop of
    another image) -- processes in the given order (PyMuPDF's own
    page.get_image_info() order), so the FIRST image to claim a region
    wins and later ones judged redundant against it are skipped.
    """
    all_boxes = []
    claimed_rects = []
    for image_no, (bbox, xref) in enumerate(images):
        rect = pymupdf.Rect(bbox)
        area = rect.width * rect.height
        if area < _MIN_IMAGE_OCR_AREA:
            continue
        overlap_area = sum((rect & claimed).get_area() for claimed in claimed_rects)
        if overlap_area / area >= _MAX_IMAGE_OVERLAP_RATIO:
            continue
        claimed_rects.append(rect)
        all_boxes.extend(
            _ocr_raw_image(fitz_page.parent, xref, rect, f"p{page_index}-ocr-img{image_no}-l")
        )
    return all_boxes

"""Google Cloud Vision OCR fallback extractor for flattened/rasterized pages.

extract.py's PyMuPDF-based extraction only finds REAL vector text -- a page
that's actually a scanned image or an export-to-image "flattened" plan (no
real text objects at all, per CLAUDE.md's original architecture note) comes
back with zero text boxes. This module is that documented fallback: render
such a page to an image, run it through Google Vision's DOCUMENT_TEXT_DETECTION,
and reconstruct the SAME JSON schema extract.py produces, so
classify_translate.py needs zero changes to work with either source -- it
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


def _line_to_text_box(page_index, line_no, text, vertices):
    xs = [v.get("x", 0) for v in vertices]
    ys = [v.get("y", 0) for v in vertices]
    x0, y0, x1, y1 = min(xs) / _RENDER_SCALE, min(ys) / _RENDER_SCALE, max(xs) / _RENDER_SCALE, max(ys) / _RENDER_SCALE
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
    rotation_angle = _rotation_angle(line_dir)

    return {
        "id": f"p{page_index}-ocr-l{line_no}",
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


def ocr_page(fitz_page, page_index):
    """Renders one page to an image, OCRs it, returns extract.py-shaped
    page JSON (every text_box carries `"source": "ocr"`)."""
    pix = fitz_page.get_pixmap(matrix=pymupdf.Matrix(_RENDER_SCALE, _RENDER_SCALE))
    image_bytes = pix.tobytes("png")

    annotation = _call_vision(image_bytes)
    text_boxes = []
    if annotation:
        for line_no, (text, vertices) in enumerate(_lines_from_annotation(annotation)):
            if not text.strip():
                continue
            text_boxes.append(
                _line_to_text_box(page_index, line_no, text, vertices)
            )

    return {
        "page_number": page_index + 1,
        "page_index": page_index,
        "width": fitz_page.rect.width,
        "height": fitz_page.rect.height,
        "text_boxes": text_boxes,
    }

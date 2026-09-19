"""Refit, redaction/reinsertion, and export — the final pipeline stages.

Consumes classify_translate.py's output (every text box already has
`pipeline.translated_text` set) plus the original PDF bytes, and produces
a translated PDF: shrink font where the translation overflows its
original footprint, wipe the original text with a redaction, draw the
translated (or unchanged) text back at the exact original position, and
re-save. Never touches the underlying diagram images.

Known-bug fixes this module exists to encode (see CLAUDE.md):
  - Reinsertion uses `page.insert_text(origin, ...)`, one call per span at
    its exact baseline origin, for every span that fits its original
    footprint — `insert_textbox` into the bbox silently draws nothing for
    tight single-line boxes, so it's never used for the normal case. The
    one exception is spans that still overflow even after the shrink
    floor (see the last bullet below), where a bounded `insert_textbox`
    is used deliberately, with its own return value checked every time to
    detect and fall back out of that same silent-nothing failure mode.
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
  - `refit_span`'s shrink loop only ever assigned `used_size` inside the
    "found a size that fits" branch, so a translation that STILL didn't
    fit at the shrink floor rendered at the full, un-shrunk original font
    size — the worst-case rendering, and the opposite of CLAUDE.md's
    "shrink to a floor before flagging" spec. Silent (no error, no test
    caught it) until French/Spanish LLM-fallback translations — which run
    longer than the hand-curated German glossary ever did — visibly ran a
    heading off the page edge. Fixed via a `while/else`: if the loop never
    breaks (never finds a fit), render at the floor size, not the original.
  - `apply_redactions()`'s default `images=2` ("blank out overlapping
    image parts") leaves a visible boundary artifact wherever a text
    span's redaction rect merely overlaps a raster image's bounding box —
    even when no diagram pixels are visibly "under" the caption text.
    Root-caused by direct pixel comparison (isolated by toggling each
    `apply_redactions` param independently against a real sample page):
    the border only appeared with `images=2`, never with `images=0` or
    `images=1`, and was present regardless of `cross_out`. This produced
    exactly the reported bug — a rectangular border baked onto the page,
    framing reinserted captions that sit near/over a diagram's raster
    image. Fixed by passing `images=0`: redaction should never touch
    image content at all here, consistent with this pipeline's existing
    "never touch the underlying diagram image" rule — text spans are the
    only thing this stage is ever supposed to rewrite. Also pass
    `add_redact_annot(..., cross_out=False)`, since the default
    `cross_out=True` appends a stroked rectangle-plus-diagonal-cross to
    the annotation's own appearance stream (see PyMuPDF's
    `Page.add_redact_annot` source) that `apply_redactions()` would
    otherwise bake permanently onto the page before deleting the
    annotation — not the cause of the reported bug on the page tested,
    but a real extra stray-ink source removed for the same reason.
  - Reinsertion hardcoded `fontname="helv"` (Helvetica) for every span,
    ignoring the span's actual extracted font entirely. Real plan PDFs use
    serif display fonts for headings (Georgia, BodoniBT) and non-Helvetica
    sans faces for body text (Calibri, Verdana) — rendering everything in
    Helvetica is visibly the wrong typeface family/weight next to
    untouched original text. Fixed by mapping the extracted font name to
    the closest PyMuPDF base-14 family+weight (`_base14_font`) instead of
    a hardcoded constant. This is a coarse substitution, not real font-
    matching (CLAUDE.md explicitly cuts embedding/matching the exact
    original typeface from MVP scope) — it only picks among the 4 base-14
    families PyMuPDF ships, but that alone fixes serif-vs-sans and
    regular-vs-bold mismatches, which was the visible complaint.
  - A translation that still overflowed even at the shrink floor was
    rendered anyway via a single `insert_text` call with no clipping —
    since `insert_text` just draws along one line with no bounds, an
    overlong line runs straight past its own column border and paints
    over whatever sits to the right (adjacent table cell text, or a
    diagram). `needs_review` was already set for this case, but the
    render itself still corrupted the layout before any human ever saw
    the flag. Fixed with a three-tier fallback for genuinely-overflowing,
    non-rotated spans, each tried in order:
      1. `_deep_shrink_fit`: keep shrinking well past CLAUDE.md's normal
         75-80% review-flagging floor (down to `_DEEP_SHRINK_FLOOR_RATIO`)
         and see if the WHOLE translation now fits back on the span's
         ORIGINAL single line. Scanning every page of the real sample PDF
         for German found 11 real overflow cases (all short labels/
         headings where German just runs longer, e.g. "Shut" ->
         "Geschlossen" in a box sized for 4 characters) — this alone
         resolves all 11 with no layout change beyond a smaller font,
         which beats either remaining option below. `pipeline.overflow`/
         `used_font_size` are left exactly as the normal floor computed
         them; only the actual render uses the deeper size.
      2. `insert_textbox` on a rect capped to the *original* bbox width
         (so it can't spill sideways) but taller (so it can wrap downward
         instead), for text too long to rescue with tier 1 alone — e.g. a
         whole extra sentence's worth. Height is sized to the number of
         lines the text actually needs (capped at
         `_OVERFLOW_WRAP_LINES_CAP`) — an early version used a flat
         multi-line-tall guess for every overflowing span, which then
         routinely overlapped the NEXT original line's own span
         immediately below (a wrapped source sentence is several
         single-line spans stacked in the original layout, not one tall
         span — extract.py emits one per original line), producing
         garbled overlapping text instead of the column-bleed it was
         meant to fix.
      3. Falls back to the original single-unclipped-line render only if
         neither of the above can place the text without clobbering a
         neighbor — still `needs_review`-flagged, so it's visible rather
         than silently dropped (`insert_textbox` draws NOTHING, not a
         partial line, when text doesn't fully fit — same failure shape
         as known bug #1).
    Normal (fitting) spans are untouched by any of this and still use the
    known-bug-1-safe `insert_text`-at-origin path.
"""

import base64
import json
import math
from http.server import BaseHTTPRequestHandler

import pymupdf

# Below this, an angle is treated as exactly axis-aligned (0/±90) when
# picking the bbox dimension to measure available space against.
_ROTATION_EPSILON_DEGREES = 0.5

# CLAUDE.md: "Shrink font down to a floor (~75-80% of original) before
# flagging for human review."
_FONT_SHRINK_STEP = 0.3
_FONT_SHRINK_FLOOR_RATIO = 0.75

# Last-resort shrink tried on a genuinely-overflowing span BEFORE wrapping
# or bleeding (see the deep-shrink step in `_insert_span`) -- deliberately
# well below CLAUDE.md's 75-80% review-flagging floor, and doesn't change
# pipeline.overflow/used_font_size (those stay exactly as the floor above
# computed them, so needs_review semantics are unaffected). Scanning every
# page of the real sample PDF for spans that still overflow at the normal
# floor found 11 cases (all short glossary-translated labels/headings
# where German simply runs longer than English, e.g. "Shut" ->
# "Geschlossen" in a box sized for 4 characters) -- 9 of 11 fit cleanly
# back onto their ORIGINAL single line at 50% of the original size, and
# tuning the ratio down to 40% (floor 6pt) fit all 11, including the
# worst case (a 33pt-wide box built for "Shut", asked to hold
# "Geschlossen"). Kept on ALL overflowing spans, not just short labels,
# since even a long heading fitting back onto its own original single
# line beats either wrapping into whatever sits directly below (often a
# diagram, not blank paper) or bleeding sideways across a column border.
_DEEP_SHRINK_FLOOR_RATIO = 0.4
_DEEP_SHRINK_MIN_SIZE = 6.0

# A span still flagged `overflow` after shrinking to the floor gets wrapped
# into a box (anchored at the original bbox's top-left, capped to the
# original bbox WIDTH) instead of overrunning past its column on a single
# unclipped line. Box height is sized to how many lines the text actually
# needs (see the wrap-height calculation in `_insert_span`), plus this much
# slack -- and never more than _OVERFLOW_WRAP_LINES_CAP lines even for
# extreme cases, since a wrapped multi-line PARAGRAPH is several
# independent spans stacked immediately below each other in the original
# layout (extract.py emits one span per original line), and a tall guess
# here would overlap the next span's own line instead of just this one's.
_OVERFLOW_WRAP_LINE_SLACK = 1
_OVERFLOW_WRAP_LINES_CAP = 4
# Empirically the minimum height/fontsize ratio `insert_textbox` needs to
# render even a single line at all -- verified across every base-14 family
# this pipeline uses (helv/hebo/tibo/cour) and several sizes by checking
# its own returned spare-space value (negative = silently drew nothing,
# the exact known-bug-1 failure mode `insert_text`-at-origin was chosen to
# avoid in the first place). 1.25x (a plausible-looking "line height")
# reliably failed this check; 1.8x reliably passed it.
_LINE_HEIGHT_RATIO = 1.8

# PyMuPDF base-14 mnemonics: family + (regular, oblique, bold, bold-oblique).
_HELVETICA = ("helv", "heit", "hebo", "hebi")
_TIMES = ("tiro", "tiit", "tibo", "tibi")
_COURIER = ("cour", "coit", "cobo", "cobi")

# Real plan PDFs found so far use font names like "Georgia-Bold",
# "BodoniBT-Bold", "Calibri", "ArialMT", "Verdana" -- substrings, not the
# generic "serif"/"sans"/"mono" labels. This list is deliberately just the
# families seen in real sample data, not an exhaustive font taxonomy.
_SERIF_MARKERS = (
    "times", "georgia", "garamond", "bodoni", "cambria", "constantia",
    "palatino", "book", "minion", "caslon", "didot", "baskerville",
    "century", "charter", "roman", "serif",
)
_MONO_MARKERS = ("courier", "consolas", "menlo", "monaco", "mono")


def _base14_font(original_font_name):
    """Maps an extracted font name to the closest PyMuPDF base-14 mnemonic.

    Coarse substitution (family + bold), not real font-matching -- CLAUDE.md
    explicitly cuts embedding/matching the exact original typeface from MVP
    scope. Picking the right one of PyMuPDF's 4 built-in families is enough
    to stop translated text from visibly rendering in the wrong type family
    (serif heading rendered as Helvetica, etc.) next to untouched original
    text using the real font.
    """
    name = (original_font_name or "").lower()
    bold = "bold" in name
    italic = "italic" in name or "oblique" in name

    if any(marker in name for marker in _MONO_MARKERS):
        family = _COURIER
    elif any(marker in name for marker in _SERIF_MARKERS):
        family = _TIMES
    else:
        family = _HELVETICA

    index = (2 if bold else 0) + (1 if italic else 0)
    return family[index]


def _text_width(text, font_size, fontname):
    return pymupdf.get_text_length(text, fontname=fontname, fontsize=font_size)


def _deep_shrink_fit(translated, original_size, fontname, available_len):
    """Last-resort single-line fit for a span already flagged `overflow`.

    Continues the SAME shrink stepping refit_span's loop already did, past
    its 75-80% review-flagging floor, down to _DEEP_SHRINK_FLOOR_RATIO /
    _DEEP_SHRINK_MIN_SIZE. Returns the largest size that fits on one line,
    or None if even the deep floor doesn't fit (genuinely too much text
    for this box, e.g. a whole extra sentence's worth -- that's when
    `_insert_span` falls through to wrapping/bleeding instead).
    """
    floor = max(original_size * _DEEP_SHRINK_FLOOR_RATIO, _DEEP_SHRINK_MIN_SIZE)
    size = original_size * _FONT_SHRINK_FLOOR_RATIO
    while size > floor:
        size -= _FONT_SHRINK_STEP
        if _text_width(translated, size, fontname) <= available_len:
            return round(size, 2)
    return None


def _available_length(text_box, fontname):
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
    return _text_width(text_box["text"], text_box["font"]["size"], fontname)


def refit_span(text_box):
    """Fills in pipeline.overflow / used_font_size / available_len.

    Escalates needs_review to True on overflow, but never clears a
    needs_review already set by classify_translate.py (e.g. an unmatched
    span is still worth a human look even if it happens to fit).
    """
    pipeline = text_box["pipeline"]
    translated = pipeline["translated_text"]
    font_size = text_box["font"]["size"]
    # Measure against the SAME font family that will actually be rendered
    # (see _base14_font) -- measuring against a hardcoded "helv" while
    # rendering in a wider/narrower substituted family (e.g. Courier) would
    # make this overflow check silently wrong for the real render.
    fontname = _base14_font(text_box["font"]["name"])
    available_len = _available_length(text_box, fontname)

    used_size = font_size
    fits = _text_width(translated, used_size, fontname) <= available_len
    if not fits:
        floor = font_size * _FONT_SHRINK_FLOOR_RATIO
        shrink = font_size
        while shrink > floor:
            shrink -= _FONT_SHRINK_STEP
            if _text_width(translated, shrink, fontname) <= available_len:
                used_size = shrink
                fits = True
                break
        else:
            # Never fit even at the floor -- render at the floor size as
            # the best available effort rather than leaving used_size at
            # the full original size, which would overflow even more.
            # Still flagged via needs_review below either way.
            used_size = floor

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


def _next_line_gap(text_box, all_boxes):
    """Vertical gap (points) to the nearest span directly below this one in
    the same column (horizontally overlapping bboxes), or None if there is
    no such neighbor on the page.

    Used to cap an overflow wrap box so growing it downward can never run
    into that neighbor's own line -- estimating the needed height from font
    metrics alone (an earlier version of this fix) still regularly landed
    taller than the ORIGINAL single-spaced line-to-line gap, since a
    wrapped source paragraph's lines sit close together relative to a
    smaller shrunk-to-floor font's own line height.
    """
    bbox = text_box["bbox"]
    x0, x1, y1 = bbox[0], bbox[2], bbox[3]
    best = None
    for other in all_boxes:
        if other is text_box:
            continue
        obbox = other["bbox"]
        if obbox[1] < y1:
            continue  # not below this span
        overlap = min(x1, obbox[2]) - max(x0, obbox[0])
        if overlap <= 0:
            continue
        gap = obbox[1] - y1
        if best is None or gap < best:
            best = gap
    return best


def _insert_span(page, text_box, next_line_gap=None):
    translated = text_box["pipeline"]["translated_text"]
    if not translated.strip():
        return

    origin = text_box["origin"]
    used_size = text_box["pipeline"]["used_font_size"]
    angle = text_box["rotation_angle"]
    color = _unpack_color(text_box["font"]["color"])
    fontname = _base14_font(text_box["font"]["name"])

    if abs(angle) >= _ROTATION_EPSILON_DEGREES:
        # Rotated overflow isn't handled by the wrap-box path below (no
        # real diagonal-translated content seen yet to validate it against
        # -- see _available_length's docstring), so this keeps the
        # existing single-line-at-origin behavior regardless of overflow.
        point = pymupdf.Point(origin)
        matrix = pymupdf.Matrix(1, 1).prerotate(angle)
        page.insert_text(
            origin, translated, fontsize=used_size, fontname=fontname, color=color,
            morph=(point, matrix),
        )
        return

    if text_box["pipeline"]["overflow"]:
        # Didn't fit even at the shrink floor. Before wrapping/bleeding,
        # try a deeper shrink that keeps it on the ORIGINAL single line --
        # this alone resolves the large majority of real overflow cases
        # (see _DEEP_SHRINK_FLOOR_RATIO's comment) with no layout change
        # at all beyond a smaller font, which beats both remaining options.
        deep_size = _deep_shrink_fit(
            translated, text_box["font"]["size"], fontname, text_box["pipeline"]["available_len"]
        )
        if deep_size is not None:
            page.insert_text(origin, translated, fontsize=deep_size, fontname=fontname, color=color)
            return
        # Still doesn't fit on one line even at the deep floor. Rendering
        # it anyway on one unclipped line would run straight past this
        # span's own column border and paint over whatever sits to the
        # right (adjacent cell text, a diagram). Wrap it into a box
        # instead: capped to the
        # ORIGINAL bbox width (can't spill sideways) and sized to just the
        # number of lines the text actually needs, capped at both
        # _OVERFLOW_WRAP_LINES_CAP and (if known) the real vertical gap to
        # the next span directly below in the same column -- a flat
        # multi-line-tall guess here would just as easily run into the
        # NEXT original line's own span immediately below, since a wrapped
        # source paragraph is several stacked single-line spans, not one
        # tall one, and their line-to-line gap is usually tighter than
        # even 2 lines' worth of a shrunk-to-floor font (confirmed by
        # testing against a real multi-span wrapped heading).
        bbox = text_box["bbox"]
        width = max(bbox[2] - bbox[0], used_size)
        line_height = used_size * _LINE_HEIGHT_RATIO
        lines_needed = math.ceil(_text_width(translated, used_size, fontname) / max(width, 1))
        lines = min(lines_needed + _OVERFLOW_WRAP_LINE_SLACK, _OVERFLOW_WRAP_LINES_CAP)
        max_height = page.rect.height - bbox[1]
        if next_line_gap is not None:
            # Small margin so the wrap box's bottom edge doesn't touch the
            # next line's own text.
            max_height = min(max_height, next_line_gap - 1)
        if max_height >= line_height:
            height = min(line_height * lines, max_height)
            rect = pymupdf.Rect(bbox[0], bbox[1], bbox[0] + width, bbox[1] + height)
            spare = page.insert_textbox(
                rect, translated, fontsize=used_size, fontname=fontname, color=color
            )
            if spare >= 0:
                return
            # insert_textbox itself reports it couldn't fit even one line
            # in the space we thought was safe -- fall through to the
            # single-line path below rather than trust a box that (per
            # known bug #1) would otherwise silently draw nothing.
        # Not enough vertical room for even one wrapped line before the
        # next span's own line starts (common for tightly-leaded original
        # headings). Rendering nothing would hide that a translation
        # exists at all; a single overlong line is still visible and
        # already flagged needs_review, so it's the safer of two
        # imperfect options here.
        page.insert_text(origin, translated, fontsize=used_size, fontname=fontname, color=color)
        return

    page.insert_text(origin, translated, fontsize=used_size, fontname=fontname, color=color)


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
        fitz_page.add_redact_annot(
            pymupdf.Rect(text_box["bbox"]), fill=(1, 1, 1), cross_out=False
        )
    # images=0: never let redaction touch image content, even a raster
    # diagram's bounding box merely overlapping a caption's text bbox (see
    # this function's module-level bug-fix note for how apply_redactions's
    # default images=2 "blank out overlapping image parts" was leaving a
    # visible boundary artifact wherever that happened -- a border baked
    # onto the page exactly at the diagram image's edge). Diagrams are
    # never supposed to be touched by this pipeline at all.
    fitz_page.apply_redactions(images=0)

    text_boxes = page_json["text_boxes"]
    for text_box in text_boxes:
        next_line_gap = (
            _next_line_gap(text_box, text_boxes) if text_box["pipeline"]["overflow"] else None
        )
        _insert_span(fitz_page, text_box, next_line_gap)

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

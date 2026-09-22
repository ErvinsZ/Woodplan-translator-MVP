"""Shared geometry helpers used by both extraction paths (extract.py's real
vector text, ocr_vision.py's Vision OCR fallback). Kept in its own module,
not in extract.py, specifically so ocr_vision.py can import it without a
circular dependency (extract.py's extract_document also needs to import
ocr_vision.py, to call it as the flattened-page fallback).
"""

import math

# Below this, a line direction is treated as exactly horizontal/vertical —
# absorbs float noise like dir=(1.0, -9.999999974752427e-07) from
# PyMuPDF's real vector line directions, which are mathematically exact.
# NOT wide enough for Vision OCR's detected word bounding boxes -- see
# ocr_vision.py's own, much wider, epsilon and why.
ROTATION_EPSILON_DEGREES = 0.5


def rotation_angle(line_dir, epsilon_degrees=ROTATION_EPSILON_DEGREES):
    """True writing-direction angle in degrees (CCW positive, 0 = horizontal).

    Real plans contain diagonal callouts (e.g. "64°", "26°" angle labels
    following a diagram's actual cut angle), not just horizontal/vertical
    text. A binary horizontal-vs-vertical model silently flattens those to
    0 degrees and loses the data needed to place them correctly on
    reinsertion — which itself must use a rotation matrix (`morph=`) for
    any non-multiple-of-90 angle, since `insert_text(rotate=...)` only
    accepts 0/90/180/270.

    `epsilon_degrees` is a parameter, not a hardcoded constant, because
    the right tolerance differs by source: PyMuPDF's vector line
    directions need only the tiny default above, but Vision's OCR word
    bounding boxes are fitted around detected ink pixels, not font
    metrics, and carry real several-degree jitter even for genuinely
    horizontal text (confirmed against a real Vision API response: up to
    6.34 degrees on an entirely horizontal real page) -- ocr_vision.py
    passes its own, much wider, value.
    """
    dx, dy = line_dir
    angle = math.degrees(math.atan2(-dy, dx))
    if abs(angle) < epsilon_degrees:
        return 0.0
    if abs(abs(angle) - 90) < epsilon_degrees:
        return 90.0 if angle > 0 else -90.0
    return round(angle, 3)

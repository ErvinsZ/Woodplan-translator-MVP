"""Shared geometry helpers used by both extraction paths (extract.py's real
vector text, ocr_vision.py's Vision OCR fallback). Kept in its own module,
not in extract.py, specifically so ocr_vision.py can import it without a
circular dependency (extract.py's extract_document also needs to import
ocr_vision.py, to call it as the flattened-page fallback).
"""

import math

# Below this, a line direction is treated as exactly horizontal/vertical —
# absorbs float noise like dir=(1.0, -9.999999974752427e-07) from PyMuPDF,
# and the equivalent from Vision's integer pixel vertex coordinates.
ROTATION_EPSILON_DEGREES = 0.5


def rotation_angle(line_dir):
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
    if abs(angle) < ROTATION_EPSILON_DEGREES:
        return 0.0
    if abs(abs(angle) - 90) < ROTATION_EPSILON_DEGREES:
        return 90.0 if angle > 0 else -90.0
    return round(angle, 3)

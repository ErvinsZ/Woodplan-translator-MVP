"""Erases text baked into an embedded raster image by inpainting it.

OCR'd text lives in the image's pixels, so there are no glyphs to redact;
painting a white box over it (the old approach) leaves visible white
rectangles on anything that isn't pure white paper -- coloured table
cells, renders, photos. Instead, like `working python script/
pdf_translate.py`, the word pixels are filled in from their surroundings
and the image is swapped for the edited copy (`page.replace_image`), so
only the translated text drawn on top is new.

The script used `cv2.inpaint`; opencv is not an option here (its ~125MB
wheel pushes every Python function past Vercel's bundle-size limit, see
CLAUDE.md), so this is a small numpy equivalent: per word, mask the ink
(pixels clearly different from the word box's own border colour), dilate
a little to catch anti-aliasing, then fill by repeated neighbour
averaging (diffusion) inside a crop around the word. For text on flat or
gently varying backgrounds -- tables, labels, callouts -- that is
visually indistinguishable from cv2's TELEA.
"""

import numpy as np

# A pixel is "ink" if its colour is this far (Euclidean, 0-255 RGB) from
# the word box's border median.
_INK_DISTANCE = 30
# If more than this share of a word box is "ink", the background is busy
# (texture/photo); erase the whole box instead of guessing strokes.
_BUSY_BACKGROUND_RATIO = 0.65
_DILATE_ITERATIONS = 3
_DIFFUSION_ITERATIONS = 60


def _dilate(mask, iterations):
    for _ in range(iterations):
        grown = mask.copy()
        grown[1:, :] |= mask[:-1, :]
        grown[:-1, :] |= mask[1:, :]
        grown[:, 1:] |= mask[:, :-1]
        grown[:, :-1] |= mask[:, 1:]
        mask = grown
    return mask


def _diffuse(crop, mask):
    """Fill crop[mask] from the unmasked surroundings."""
    out = crop.astype(np.float32)
    if (~mask).any():
        out[mask] = np.median(out[~mask], axis=0)
    for _ in range(_DIFFUSION_ITERATIONS):
        p = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge")
        avg = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) / 4
        out[mask] = avg[mask]
    return out.clip(0, 255).astype(np.uint8)


def erase_words(pixels, word_boxes):
    """Returns a copy of `pixels` (HxWx3 uint8) with the text inside each
    (x0, y0, x1, y1) word box (image pixel space) inpainted away."""
    img = pixels.copy()
    H, W = img.shape[:2]
    mask = np.zeros((H, W), bool)
    regions = []
    for x0, y0, x1, y1 in word_boxes:
        pad = max(2.0, 0.15 * (y1 - y0))
        xi0, yi0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
        xi1, yi1 = int(min(W, x1 + pad + 1)), int(min(H, y1 + pad + 1))
        if xi1 <= xi0 or yi1 <= yi0:
            continue
        box = img[yi0:yi1, xi0:xi1].astype(float)
        ring = np.concatenate([box[0], box[-1], box[:, 0], box[:, -1]])
        bg = np.median(ring, axis=0)
        ink = np.linalg.norm(box - bg, axis=2) > _INK_DISTANCE
        if ink.mean() > _BUSY_BACKGROUND_RATIO:
            ink[:] = True
        mask[yi0:yi1, xi0:xi1] |= ink
        margin = _DILATE_ITERATIONS + 6
        regions.append((max(0, xi0 - margin), max(0, yi0 - margin),
                        min(W, xi1 + margin), min(H, yi1 + margin)))
    mask = _dilate(mask, _DILATE_ITERATIONS)
    for x0, y0, x1, y1 in regions:
        m = mask[y0:y1, x0:x1]
        if m.any():
            img[y0:y1, x0:x1] = _diffuse(img[y0:y1, x0:x1], m)
    return img

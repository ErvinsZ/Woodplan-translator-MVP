"""Classify/translate stage -- consumes extract.py's JSON, fills in each
translation UNIT's `pipeline` (see layout_units.py for what a unit is).

Translation happens per unit PARAGRAPH, never per span: a wrapped
sentence is one paragraph across several spans/lines, and translating the
pieces independently is what produced uneven word splits and mid-word
breaks. Tiers, cheapest first:

  1. Nothing translatable (numbers, dimensions, part codes) -> unchanged.
  2. Whole paragraph matches a static glossary entry (case-insensitive).
  3. Every LINE of the paragraph matches a glossary entry -> joined. The
     static glossary was built span-by-span from the sample PDF (entries
     like "Do not share the files with anyone without " carry their
     original whitespace -- known bug #3), so this keeps that coverage
     for multi-line paragraphs. Raw line text is tried before stripped.
  4. Per-token: codes pass through, every word must be a glossary hit
     (known bug #5 -- "S2 screws, 16 pc." is one span).
  5. Whole-document LLM pass: remaining paragraphs are deduped, checked
     against the Postgres cache (translation_cache.py), and the rest sent
     to Gemini with full-sentence context -- numbers/codes MASKED first
     (masking.py) so they can't be altered. A failure falls back to a
     tier-4 partial translation if there is one, else leaves the
     paragraph in the original language; either way needs_review=True.

Per-span text_boxes keep their schema: each gets `pipeline.unit_id` and
the unit's classification/needs_review; `translated_text` stays None for
spans inside a unit (translation is paragraph-level now) and equals the
original text for spans that are never touched.
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

# Vercel's Python runtime loads the entrypoint via importlib (exec_module),
# not as a directly-run script, so this file's own directory is never
# auto-added to sys.path the way `python classify_translate.py` would --
# sibling imports silently 404 with ModuleNotFoundError without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from glossary import GLOSSARY
from layout_units import needs_translation, split_prefix
from masking import mask, numbers_preserved, unmask
import gemini_translate
import translation_cache

# Numbers, dimension strings ("3'-1 3/8\""), and part codes (D1, S2) --
# never translated, regardless of glossary contents.
CODE_TOKEN = re.compile(r'^(D\d+|S\d+|\d+[\d\s\'"/.°xX×-]*|[0-9]+)$')

# Lowest Vision symbol confidence at which OCR'd digits in a translated
# unit are trusted without human review (same bar the reference script
# used to redraw raster numbers as vector text).
_OCR_DIGIT_MIN_CONFIDENCE = 0.9

# Splits on whitespace and commas while keeping the separators, so a
# translated span can be reassembled with its original spacing/punctuation.
_TOKEN_SPLIT = re.compile(r'(\s+|,)')

# Case-insensitive index: lowercased source text -> canonical GLOSSARY key.
_LOWER_INDEX = {source.lower(): source for source in GLOSSARY}
# Same, keyed on the STRIPPED source (paragraph text is whitespace-normalized).
_STRIPPED_INDEX = {source.strip().lower(): source for source in GLOSSARY}


def _is_code(token):
    if CODE_TOKEN.match(token):
        return True
    # "D1-A.", "N2.", "4.2" -- nothing left but punctuation once masking.py's
    # number/part-code tokens are removed.
    masked, tokens = mask(token)
    return bool(tokens) and not any(c.isalpha() for c in re.sub(r"⟦\d+⟧", "", masked))


def _is_translatable_word(token):
    """A token worth flagging as untranslated: has a letter, isn't a code."""
    return bool(token) and any(c.isalpha() for c in token) and not _is_code(token)


def _match_case(source, translation):
    """Lookups are case-insensitive, so an ALL-CAPS source ("STEP 2.")
    can hit a mixed-case entry ("Step 2. " -> "Schritt 2. "); keep the
    source's all-caps styling."""
    letters = [c for c in source if c.isalpha()]
    if translation is not None and len(letters) >= 2 and all(c.isupper() for c in letters):
        return translation.upper()
    return translation


def _glossary_lookup(key, target_lang):
    canonical = _LOWER_INDEX.get(key.lower())
    if canonical is None:
        return None
    return _match_case(key, GLOSSARY[canonical].get(target_lang))


def _reapply_whitespace(original_text, translation):
    """Reapplies original_text's leading/trailing whitespace around a
    translation produced from its stripped form."""
    leading_ws = original_text[: len(original_text) - len(original_text.lstrip())]
    trailing_ws = original_text[len(original_text.rstrip()):]
    return leading_ws + translation + trailing_ws


def _match_whole_span(text, target_lang):
    """Raw text first (exact whitespace match), then stripped-with-reapplied
    whitespace (known bug #3)."""
    raw_translation = _glossary_lookup(text, target_lang)
    if raw_translation is not None:
        return raw_translation

    stripped = text.strip()
    if stripped != text:
        stripped_translation = _glossary_lookup(stripped, target_lang)
        if stripped_translation is not None:
            return _reapply_whitespace(text, stripped_translation)

    return None


def _match_paragraph(text, target_lang):
    canonical = _STRIPPED_INDEX.get(text.strip().lower())
    if canonical is None:
        return None
    translation = _match_case(text, GLOSSARY[canonical].get(target_lang))
    return translation.strip() if translation is not None else None


def _match_lines(raw_lines, target_lang):
    """Tier 3: every line is a glossary hit -> joined translation."""
    parts = []
    for raw in raw_lines:
        translation = _match_whole_span(raw, target_lang)
        if translation is None:
            return None
        parts.append(translation.strip())
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p))


def _match_tokenized(text, target_lang):
    """Tier 4: split into words/codes/punctuation, translate what's covered."""
    parts = _TOKEN_SPLIT.split(text)
    out = []
    any_translated = False
    any_unmatched_word = False

    for part in parts:
        key = part.strip()
        if not key:
            out.append(part)
            continue
        if _is_code(key):
            out.append(part)
            continue
        translation = _glossary_lookup(key, target_lang)
        if translation is None and key.endswith("."):
            # "pc." / "pcs." -- glossary keys are the bare word.
            translation = _glossary_lookup(key[:-1], target_lang)
            if translation is not None and not translation.endswith("."):
                translation += "."
        if translation is not None:
            out.append(translation)
            any_translated = True
        else:
            out.append(part)
            if _is_translatable_word(key):
                any_unmatched_word = True

    return "".join(out), any_translated, any_unmatched_word


def classify_and_translate(text, target_lang, raw_lines=None):
    """Tiers 1-4 for one paragraph body. Returns (translation,
    classification, partial): translation is None when only the LLM tier
    can help; partial is a tier-4 best effort to fall back on then."""
    stripped = text.strip()
    if not stripped:
        return text, "blank", None
    if not needs_translation(stripped) or _is_code(stripped):
        return text, "code", None

    whole = _match_paragraph(stripped, target_lang)
    if whole is not None:
        return whole, "glossary_match", None

    if raw_lines and len(raw_lines) > 1:
        joined = _match_lines(raw_lines, target_lang)
        if joined is not None:
            return joined, "glossary_match", None

    translated, any_translated, any_unmatched_word = _match_tokenized(stripped, target_lang)
    if any_translated and not any_unmatched_word:
        return translated, "mixed", None
    return None, "unmatched", translated if any_translated else None


def _llm_translate(texts, target_lang):
    """{stripped_text: translation} via cache, then masked Gemini."""
    by_key = {}
    for text in texts:
        by_key.setdefault(text.lower(), text)
    if not by_key:
        return {}

    results = {}
    cached = translation_cache.lookup_many(by_key.values(), target_lang)
    for key, translation in cached.items():
        if key in by_key and numbers_preserved(by_key[key], translation):
            results[key] = translation

    misses = [key for key in by_key if key not in results]
    if not misses:
        return results
    # Texts that differ only in their numbers ("STEP 1.", "STEP 2.") mask
    # to the same string: send it once, so they're translated identically
    # (and cheaper).
    masked = {key: mask(by_key[key]) for key in misses}
    unique = list(dict.fromkeys(masked_text for masked_text, _ in masked.values()))
    try:
        gemini_results = gemini_translate.translate_batch(list(enumerate(unique)), target_lang)
    except Exception as exc:
        print(f"classify_translate: LLM fallback failed ({exc!r})", file=sys.stderr)
        return results
    by_masked = {
        unique[i]: raw for i, raw in gemini_results.items()
        if isinstance(i, int) and 0 <= i < len(unique)
    }

    new_entries = []
    for key, (masked_text, tokens) in masked.items():
        raw = by_masked.get(masked_text)
        if raw is None:
            continue
        restored = unmask(raw, tokens)
        if restored is None:
            print(
                f"classify_translate: rejected translation with altered placeholders: {raw!r}",
                file=sys.stderr,
            )
            continue
        results[key] = restored
        new_entries.append((by_key[key], restored))
    translation_cache.save_many(new_entries, target_lang, gemini_translate.MODEL)
    return results


def classify_translate_document(document, target_lang):
    """Fills in `pipeline` for every unit (and text box) of an extract.py document."""
    pending = []  # (unit, paragraph_index, prefix, body, partial)
    for page in document["pages"]:
        box_by_id = {tb["id"]: tb for tb in page["text_boxes"]}
        in_unit = set()
        for unit in page.get("units", []):
            translated = []
            classes = []
            for i, para in enumerate(unit["paragraphs"]):
                prefix, body = split_prefix(para["text"])
                raw_lines = None if prefix else [
                    unit["lines"][li]["raw_text"] for li in para["line_indexes"]
                ]
                translation, classification, partial = classify_and_translate(
                    body, target_lang, raw_lines
                )
                classes.append(classification)
                translated.append(prefix + translation if translation is not None else None)
                if translation is None:
                    pending.append((unit, i, prefix, body, partial))
            pipeline = unit["pipeline"]
            pipeline["target_lang"] = target_lang
            pipeline["translated_paragraphs"] = translated
            pipeline["classification"] = (
                "unmatched" if "unmatched" in classes else
                "mixed" if "mixed" in classes else
                "glossary_match" if "glossary_match" in classes else "code"
            )
            for line in unit["lines"]:
                in_unit.update(line["box_ids"])
                for box_id in line["box_ids"]:
                    box_by_id[box_id]["pipeline"]["unit_id"] = unit["id"]
        for tb in page["text_boxes"]:
            if tb["id"] not in in_unit:
                tb["pipeline"].update(
                    classification="code", target_lang=target_lang, translated_text=tb["text"]
                )

    if pending:
        llm = _llm_translate([body.strip() for _, _, _, body, _ in pending], target_lang)
        for unit, i, prefix, body, partial in pending:
            translation = llm.get(body.strip().lower())
            pipeline = unit["pipeline"]
            if translation is not None:
                pipeline["translated_paragraphs"][i] = prefix + translation
                if pipeline["classification"] == "unmatched":
                    pipeline["classification"] = "llm_match"
            else:
                pipeline["translated_paragraphs"][i] = prefix + (partial if partial else body)
                pipeline["classification"] = "unmatched"
                pipeline["needs_review"] = True

    for page in document["pages"]:
        for unit in page.get("units", []):
            # A translated OCR unit is redrawn from Vision's reading, digits
            # included -- unlike vector text, those digits aren't guaranteed
            # to match the source. Flag it unless Vision was confident.
            if unit["source"] == "ocr" and any(
                re.search(r"\d", t or "") for t in unit["pipeline"]["translated_paragraphs"] or []
            ) and min(l["ocr"]["min_conf"] for l in unit["lines"]) < _OCR_DIGIT_MIN_CONFIDENCE:
                unit["pipeline"]["needs_review"] = True

        units = {u["id"]: u for u in page.get("units", [])}
        for tb in page["text_boxes"]:
            unit = units.get(tb["pipeline"].get("unit_id"))
            if unit is not None:
                tb["pipeline"].update(
                    classification=unit["pipeline"]["classification"],
                    target_lang=target_lang,
                    needs_review=unit["pipeline"]["needs_review"],
                )
    return document


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point.

    POST JSON: {"target_lang": "de", "document": <extract.py output>}.
    Returns the same shape with every unit's `pipeline` filled in.
    """

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)
        result = classify_translate_document(payload["document"], payload["target_lang"])

        response = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(response)

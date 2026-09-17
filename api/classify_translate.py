"""Classify/translate stage — consumes extract.py's JSON, fills in `pipeline`.

For each text box, decides whether it's a numeric/part code (never
translated), a whole-span glossary match, a mixed span needing per-token
translation, or a span with no glossary coverage at all. No LLM/MT call
happens here: anything not covered by the glossary is left untouched with
needs_review=True (that fallback is a later feature, not this stage's job).

Known-bug fixes this module exists to encode (see CLAUDE.md):
  - Glossary lookup tries the RAW (whitespace-preserving) span text
    first. Some glossary keys carry meaningful leading/trailing
    whitespace because the source PDF splits one line-wrapped sentence
    across two separate text spans — stripping before the first lookup
    attempt silently misses those (this is the bug pipeline_tested.py
    itself still has: it strips before ever checking the dict, so its
    two whitespace-bearing entries can never match). Only after the raw
    lookup misses do we fall back to a stripped lookup with the
    original leading/trailing whitespace reapplied around the result.
  - Whole-span glossary phrases are checked BEFORE falling back to
    per-word tokenization, and matching is case-insensitive.
  - Mixed spans — a part code, a translatable word, and a count sharing
    one PyMuPDF text object, e.g. "S2 screws, 16 pc." — are tokenized
    rather than translated or skipped wholesale, so codes pass through
    exactly and words translate independently within the same span.
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

# Numbers, dimension strings ("3'-1 3/8\""), and part codes (D1, S2) —
# never translated, regardless of glossary contents.
CODE_TOKEN = re.compile(r'^(D\d+|S\d+|\d+[\d\s\'"/.°xX×-]*|[0-9]+)$')

# Splits on whitespace and commas while keeping the separators, so a
# translated span can be reassembled with its original spacing/punctuation.
_TOKEN_SPLIT = re.compile(r'(\s+|,)')

# Case-insensitive index: lowercased source text -> canonical GLOSSARY key.
_LOWER_INDEX = {source.lower(): source for source in GLOSSARY}


def _is_code(token):
    return bool(CODE_TOKEN.match(token))


def _is_translatable_word(token):
    """A token worth flagging as untranslated: has a letter, isn't a code."""
    return bool(token) and any(c.isalpha() for c in token) and not _is_code(token)


def _glossary_lookup(key, target_lang):
    canonical = _LOWER_INDEX.get(key.lower())
    if canonical is None:
        return None
    return GLOSSARY[canonical].get(target_lang)


def _match_whole_span(text, target_lang):
    """Tier 1: try the whole span as one glossary phrase.

    Raw text first (exact whitespace match), then stripped-with-reapplied
    whitespace as a fallback for spans whose surrounding whitespace isn't
    semantically part of a glossary key.
    """
    raw_translation = _glossary_lookup(text, target_lang)
    if raw_translation is not None:
        return raw_translation

    stripped = text.strip()
    if stripped != text:
        stripped_translation = _glossary_lookup(stripped, target_lang)
        if stripped_translation is not None:
            leading_ws = text[: len(text) - len(text.lstrip())]
            trailing_ws = text[len(text.rstrip()):]
            return leading_ws + stripped_translation + trailing_ws

    return None


def _match_tokenized(text, target_lang):
    """Tier 2: split into words/codes/punctuation, translate what's covered."""
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
        if translation is not None:
            out.append(translation)
            any_translated = True
        else:
            out.append(part)
            if _is_translatable_word(key):
                any_unmatched_word = True

    return "".join(out), any_translated, any_unmatched_word


def classify_and_translate(text, target_lang):
    """Returns (translated_text, classification, needs_review)."""
    stripped = text.strip()

    if not stripped:
        return text, "blank", False

    if _is_code(stripped):
        return text, "code", False

    whole_match = _match_whole_span(text, target_lang)
    if whole_match is not None:
        return whole_match, "glossary_match", False

    translated, any_translated, any_unmatched_word = _match_tokenized(text, target_lang)
    if any_translated:
        return translated, "mixed", any_unmatched_word

    # No exact-phrase and no per-token coverage: leave untouched, flag it.
    return text, "unmatched", True


def classify_translate_page(page, target_lang):
    """Fills in `pipeline` for every text box on one extract.py page dict."""
    for text_box in page["text_boxes"]:
        translated, classification, needs_review = classify_and_translate(
            text_box["text"], target_lang
        )
        text_box["pipeline"]["classification"] = classification
        text_box["pipeline"]["target_lang"] = target_lang
        text_box["pipeline"]["translated_text"] = translated
        text_box["pipeline"]["needs_review"] = needs_review
    return page


def classify_translate_document(document, target_lang):
    """Fills in `pipeline` for every text box across an extract.py document dict."""
    for page in document["pages"]:
        classify_translate_page(page, target_lang)
    return document


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point.

    POST JSON: {"target_lang": "de", "document": <extract.py output>}
    or {"target_lang": "de", "page": <single extract.py page dict>}.
    Returns the same shape with every text box's `pipeline` field filled in.
    """

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)
        target_lang = payload["target_lang"]

        if "document" in payload:
            result = classify_translate_document(payload["document"], target_lang)
        else:
            result = classify_translate_page(payload["page"], target_lang)

        response = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(response)

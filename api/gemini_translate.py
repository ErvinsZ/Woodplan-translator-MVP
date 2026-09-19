"""Gemini API client for whole-sentence translation fallback.

Only called for spans neither the static glossary (api/glossary.py) nor
the dynamic cache (api/translation_cache.py) cover. Always sends full-
sentence context -- never called per-word -- specifically to resolve
homographs correctly ("board" a piece of lumber vs. "board" a ship),
which is the whole reason a naive word-by-word fallback would be worse
than no fallback at all.

Model: gemini-3.1-flash-lite (Google AI Studio free tier). NOTE:
gemini-2.5-flash-lite -- the model originally targeted -- returned a 404
("no longer available to new users") on this project's API key, despite
still appearing in the /models listing; gemini-3.1-flash-lite is what this
key's own AI Studio dashboard actually shows as available, confirmed
2026-09-19: 15 requests/min, 500 requests/day, 250,000 tokens/min. Google
has changed free-tier model availability and quotas before -- re-check the
live AI Studio dashboard (not just this comment) if these numbers ever
look wrong, and don't assume a model name from training data or search
results is actually usable without checking the real /models response or
the dashboard first.

Rate limiting here is IN-PROCESS ONLY: it keeps a single
`translate_batch` call under the per-minute ceiling by itself, with no
coordination across concurrent invocations. That's a deliberate MVP-scope
decision (see PROJECT_STATE.md) -- there's no auth/multi-tenancy yet, so
there's no real concurrent load to coordinate against. Revisit with a
shared limiter (e.g. Upstash Redis) once feature/auth ships.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google import genai
from google.genai import errors, types

from glossary import GLOSSARY

MODEL = "gemini-3.1-flash-lite"

_LANGUAGE_NAMES = {"de": "German", "es": "Spanish", "fr": "French"}

# Conservative per-call token budget -- well under the 250k TPM free-tier
# ceiling, leaving headroom for other documents processed close together.
# chars/4 is a rough, deliberately-generous estimate, not exact
# tokenization -- doesn't need to be exact, just safely conservative.
_MAX_TOKENS_PER_CHUNK = 50_000
_CHARS_PER_TOKEN_ESTIMATE = 4

# Minimum spacing between sequential Gemini calls within one invocation,
# so a document needing multiple chunks still can't exceed 15 req/min by
# itself. The common case (per PROJECT_STATE.md's own numbers -- 4 of 533
# spans unmatched with 114 glossary entries) is a single chunk, i.e. this
# never triggers.
_MIN_SECONDS_BETWEEN_CALLS = 4.5

_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (2, 6)
_RETRYABLE_CODES = (429, 500, 502, 503, 504)

_RESPONSE_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "translation": {"type": "string"},
        },
        "required": ["id", "translation"],
    },
}


def _client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def _system_instruction(target_lang):
    language = _LANGUAGE_NAMES[target_lang]
    glossary_lines = [
        f'"{source}" -> "{entry[target_lang]}"'
        for source, entry in GLOSSARY.items()
        if target_lang in entry
    ]
    glossary_block = "\n".join(glossary_lines) if glossary_lines else "(none yet)"

    return (
        "You are translating technical woodworking/DIY build-plan "
        f"instructions from English into {language}. The register is "
        "short, technical, and instructional, matching printed plan "
        'conventions (imperative sentences, headings like "Cutout List" '
        'or "Top view").\n\n'
        "Translate only the text given. Never alter, add, or remove "
        "numbers, dimensions, or part codes (e.g. \"D1\", \"S2\", "
        "\"3'-1 3/8\\\"\"); a wrong digit in a woodworking dimension can "
        "cause wasted materials or a safety issue.\n\n"
        "For consistency, these English terms already have an "
        f"established {language} translation in this project's glossary "
        "-- use the same term whenever it applies, including to "
        "disambiguate words with more than one meaning (e.g. \"board\" as "
        'a piece of lumber vs. "board" a ship):\n\n'
        f"{glossary_block}\n\n"
        'You will receive a JSON array of {"id": <int>, "text": <string>} '
        "objects. Return a JSON array of the exact same length, one "
        '{"id": <int>, "translation": <string>} object per input id, in '
        "any order. Do not merge, split, skip, or add entries."
    )


def _estimate_tokens(text):
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE)


def _chunk(pairs):
    chunk, chunk_tokens = [], 0
    for item_id, text in pairs:
        tokens = _estimate_tokens(text)
        if chunk and chunk_tokens + tokens > _MAX_TOKENS_PER_CHUNK:
            yield chunk
            chunk, chunk_tokens = [], 0
        chunk.append((item_id, text))
        chunk_tokens += tokens
    if chunk:
        yield chunk


def _call_gemini(client, system_instruction, chunk):
    contents = json.dumps(
        [{"id": item_id, "text": text} for item_id, text in chunk],
        ensure_ascii=False,
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_json_schema=_RESPONSE_JSON_SCHEMA,
        ),
    )
    parsed = json.loads(response.text)
    return {item["id"]: item["translation"] for item in parsed}


def translate_batch(pairs, target_lang):
    """pairs: list of (id, stripped_text).

    Returns {id: translation} for whatever Gemini successfully
    translated. Ids missing from the result (failed chunk, bad response)
    are the caller's signal to fall back to needs_review -- this function
    never raises for a single chunk's failure, only for setup errors
    (missing API key) that make every call impossible.
    """
    if not pairs:
        return {}

    client = _client()
    system_instruction = _system_instruction(target_lang)
    results = {}
    last_call_at = 0.0

    for chunk in _chunk(pairs):
        wait = _MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - last_call_at)
        if wait > 0:
            time.sleep(wait)

        for attempt in range(_MAX_RETRIES + 1):
            last_call_at = time.monotonic()
            try:
                results.update(_call_gemini(client, system_instruction, chunk))
                break
            except (errors.APIError, json.JSONDecodeError, KeyError, TypeError) as exc:
                code = getattr(exc, "code", None)
                retryable = code in _RETRYABLE_CODES
                if not retryable or attempt == _MAX_RETRIES:
                    print(
                        f"gemini_translate: chunk of {len(chunk)} span(s) failed "
                        f"({exc!r}), leaving for needs_review fallback",
                        file=sys.stderr,
                    )
                    break
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

    return results

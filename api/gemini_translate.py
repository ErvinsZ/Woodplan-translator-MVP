"""Gemini API client for whole-sentence translation fallback.

Only called for spans neither the static glossary (api/glossary.py) nor
the dynamic cache (api/translation_cache.py) cover. Always sends full-
sentence context -- never called per-word -- specifically to resolve
homographs correctly ("board" a piece of lumber vs. "board" a ship),
which is the whole reason a naive per-word fallback would be worse than
no fallback at all.

Talks to the REST API directly via urllib rather than the official
`google-genai` SDK -- same reasoning as api/blob.py's hand-rolled Vercel
Blob client: `google-genai` pulls in `pydantic`/`pydantic-core`, which
ships a compiled Rust extension. That crashed every invocation of this
whole pipeline with `ModuleNotFoundError: No module named
'pydantic_core._pydantic_core'` (found via `vercel dev`, confirmed to be
a known, recurring, unresolved issue on real Vercel Python deployments
too, not just local emulation -- see Vercel community/pydantic GitHub
threads). The REST contract itself is simple (one POST, JSON in/out) and
was verified directly with curl before writing this, the same way
blob.py's contract was reverse-engineered rather than guessed.

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
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import certifi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from glossary import GLOSSARY

MODEL = "gemini-3.1-flash-lite"
_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Same certifi-based SSL context as blob.py, for the same reason: some
# Python-runtime environments don't have a system CA bundle linked.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

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
_REQUEST_TIMEOUT_SECONDS = 60

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


class _GeminiError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Gemini API error {code}: {message}")
        self.code = code


def _api_key():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return api_key


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


def _call_gemini(api_key, system_instruction, chunk):
    contents_text = json.dumps(
        [{"id": item_id, "text": text} for item_id, text in chunk],
        ensure_ascii=False,
    )
    payload = {
        "contents": [{"parts": [{"text": contents_text}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": _RESPONSE_JSON_SCHEMA,
        },
    }
    url = f"{_API_URL}/{MODEL}:generateContent?key={urllib.parse.quote(api_key)}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, context=_SSL_CONTEXT, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise _GeminiError(exc.code, exc.read().decode()) from exc

    text = body["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
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

    api_key = _api_key()
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
                results.update(_call_gemini(api_key, system_instruction, chunk))
                break
            except (
                _GeminiError,
                urllib.error.URLError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
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

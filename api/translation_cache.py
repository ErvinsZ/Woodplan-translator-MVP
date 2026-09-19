"""Dynamic (Gemini-generated) translation memory -- Postgres-backed.

Distinct from api/glossary.py's hand-curated, checked-in GLOSSARY dict:
this table only holds translations Gemini produced at runtime, keyed on
STRIPPED, LOWERCASED source text -- not raw/whitespace-preserving like the
static glossary. That's a deliberate deviation: static entries are
hand-tuned against specific known PDF spans where whitespace is load-
bearing (see classify_translate.py's known-bug #3 handling); dynamic
entries are generated ad hoc per novel sentence, where whitespace variants
of the same sentence should collapse to one cache row instead of
fragmenting it. Callers reapply the original span's leading/trailing
whitespace after a hit (classify_translate.py's `_reapply_whitespace`).

Any DB failure here is caught and surfaces as an empty result / no-op
save -- persistence is an optimization on top of the Gemini fallback, not
a correctness requirement. A cache-unavailable document still translates
correctly, it just doesn't get the reuse benefit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_connection


def lookup_many(stripped_texts, target_lang):
    """stripped_texts: iterable of already-stripped source strings.

    Returns {lowercased_text: translation} for whatever's cached. Missing
    keys simply aren't in the result -- callers treat that as a cache miss,
    not an error.
    """
    normalized = {text.lower() for text in stripped_texts if text}
    if not normalized:
        return {}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_text_lower, translation
                    FROM glossary_terms
                    WHERE source_text_lower = ANY(%s) AND target_lang = %s
                    """,
                    (list(normalized), target_lang),
                )
                return dict(cur.fetchall())
    except Exception as exc:
        print(f"translation_cache: lookup failed ({exc!r}), treating as cache miss", file=sys.stderr)
        return {}


def save_many(entries, target_lang, model_name):
    """entries: iterable of (stripped_source_text, translation) tuples.

    Upserts nothing on conflict -- first writer wins. A human can hand-edit
    a row later by (source_text_lower, target_lang) once any review
    tooling exists; not needed for v1.
    """
    rows = [
        (source, source.lower(), target_lang, translation, model_name)
        for source, translation in entries
        if source
    ]
    if not rows:
        return

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO glossary_terms
                        (source_text, source_text_lower, target_lang, translation, model)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (source_text_lower, target_lang) DO NOTHING
                    """,
                    rows,
                )
            conn.commit()
    except Exception as exc:
        print(f"translation_cache: save failed ({exc!r}), translations still applied this run", file=sys.stderr)

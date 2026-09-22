"""Hides numbers, dimensions and part codes from the LLM.

CLAUDE.md: numbers/codes must NEVER pass through translation -- a wrong
digit in a dimension wastes material or is a safety issue. Sending whole
sentences to Gemini (needed for context) used to include them verbatim;
the model was only asked nicely to leave them alone. The reference script
did the same, and 10 of its 754 cached translations came back with
changed numbers or units ("4.63 m²" -> "4,63 m²", "ft'" -> "pi²",
"I set" -> "1 Satz").

`mask` swaps every such token for a ⟦N⟧ placeholder; `unmask` puts the
exact original bytes back and REJECTS the translation (returns None) if
any placeholder went missing, got duplicated, or was invented -- so a
translation either carries every original number byte-for-byte or isn't
used at all.
"""

import re

# A number/dimension run: digits joined by . , / : x × ° ' " - and single
# spaces between digit groups (3'-1 3/8", 6 x 40, 1:10, 17.64), plus an
# optional trailing metric/imperial unit so the unit can't be "localized"
# either. "pc"/"pcs" are deliberately NOT included -- those translate.
_NUMBER = (
    r"[+±]?\d(?:[\d.,/:×x°'\"\-]|\s(?=[\dx×]))*"
    r"(?:\s?(?:mm|cm|m²|m³|m2|m3|m|ft²|ft³|ft'|ft|kg)(?![A-Za-z]))?"
)
# Part codes: D1, S2, N3, D1-A, D10-B.
_CODE = r"\b[A-Z]{1,3}\d+[A-Za-z]?(?:-[A-Z0-9]+)*\b"
_TOKEN_RE = re.compile(f"{_CODE}|{_NUMBER}")
_PLACEHOLDER_RE = re.compile(r"⟦(\d+)⟧")


def mask(text):
    """Returns (masked_text, tokens)."""
    tokens = []

    def repl(m):
        # Sentence punctuation after a number ("in STEP 4.") stays in the
        # text, so the model can move it with the sentence.
        token = m.group(0)
        core = token.rstrip(".,:;-/x×")
        tokens.append(core)
        return f"⟦{len(tokens) - 1}⟧" + token[len(core):]

    return _TOKEN_RE.sub(repl, text), tokens


def numbers_preserved(source, translation):
    """True if `translation` carries exactly the same number/code tokens as
    `source` -- used to vet translations from before masking existed
    (e.g. older glossary_terms cache rows)."""
    return sorted(mask(source)[1]) == sorted(mask(translation)[1])


def unmask(translated, tokens):
    """Restores placeholders, or returns None if they don't match 1:1."""
    found = [int(n) for n in _PLACEHOLDER_RE.findall(translated)]
    if sorted(found) != list(range(len(tokens))):
        return None
    return _PLACEHOLDER_RE.sub(lambda m: tokens[int(m.group(1))], translated)

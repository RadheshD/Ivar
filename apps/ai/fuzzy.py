"""Standard-library fuzzy matching (difflib) for typos such as "makana" or "protien"."""

from __future__ import annotations

import difflib

from .rules import KNOWN_WORDS, Vocabulary

# 0.85 keeps "makana"/"protien"/"paymnet" (>= 0.857) and rejects "light"->"night" (0.8).
WORD_CUTOFF = 0.85
MIN_WORD_LENGTH = 4

_LATIN_TARGETS = tuple(sorted(w for w in KNOWN_WORDS if w.isascii() and w.isalpha()))


def correct_tokens(tokens: list[str], vocab: Vocabulary) -> tuple[list[str], bool]:
    """Replace unknown Latin words with their closest known word, if close enough."""
    targets = _LATIN_TARGETS + tuple(w for w in vocab.hint_words if w.isascii() and w.isalpha())
    corrected: list[str] = []
    changed = False
    for token in tokens:
        if (
            len(token) >= MIN_WORD_LENGTH
            and token.isascii()
            and token.isalpha()
            and not vocab.knows(token)
        ):
            close = difflib.get_close_matches(token, targets, n=1, cutoff=WORD_CUTOFF)
            if close:
                corrected.append(close[0])
                changed = True
                continue
        corrected.append(token)
    return corrected, changed

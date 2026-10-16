"""Standard-library fuzzy matching (difflib) for typos such as "makana" or "protien".

``IVA_FUZZY_NUMBER_GUARD`` (on by default): a word is never corrected INTO a number word, so
"weight" can no longer become "eight" and quantity 8. Any fuzzy cart change still asks Yes/No
first either way.
"""

from __future__ import annotations

import difflib

from django.conf import settings

from .aliases import NUMBER_WORDS
from .code_cues import FUZZY_STOPLIST
from .rules import KNOWN_WORDS, Vocabulary
from .text import normalize

# 0.85 keeps "makana"/"protien"/"paymnet" (>= 0.857) and rejects "light"->"night" (0.8).
WORD_CUTOFF = 0.85
MIN_WORD_LENGTH = 4

_NUMBER_WORDS = frozenset(normalize(w) for w in NUMBER_WORDS)
_LATIN_TARGETS = tuple(sorted(w for w in KNOWN_WORDS if w.isascii() and w.isalpha()))


def correct_tokens(tokens: list[str], vocab: Vocabulary) -> tuple[list[str], bool]:
    """Replace unknown Latin words with their closest known word, if close enough."""
    guard = getattr(settings, "IVA_FUZZY_NUMBER_GUARD", True)
    targets = tuple(
        w
        for w in _LATIN_TARGETS + tuple(w for w in vocab.hint_words if w.isascii() and w.isalpha())
        if not guard or w not in _NUMBER_WORDS  # a corrected word must never become a quantity
    )
    corrected: list[str] = []
    changed = False
    for token in tokens:
        if (
            len(token) >= MIN_WORD_LENGTH
            and token.isascii()
            and token.isalpha()
            and not vocab.knows(token)
            and token not in FUZZY_STOPLIST
        ):
            close = difflib.get_close_matches(token, targets, n=1, cutoff=WORD_CUTOFF)
            if close:
                corrected.append(close[0])
                changed = True
                continue
        corrected.append(token)
    return corrected, changed

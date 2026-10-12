"""Quantity extraction: digits (any script), number words in three languages and units.

No maximum is enforced here; the Backend Port owns quantity limits (``InvalidQuantity``).
Digit runs longer than four are ignored so phone and order numbers never become
quantities.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import aliases
from .text import normalize, tokenize, word_matches

MAX_DIGITS = 4

_NUMBER_WORDS = {normalize(k): v for k, v in aliases.NUMBER_WORDS.items()}
_AMBIGUOUS = frozenset(normalize(w) for w in aliases.AMBIGUOUS_NUMBER_WORDS)
_UNITS = tuple((normalize(w), unit) for unit, words in aliases.UNITS.items() for w in words)
_PACK_SIZE_UNITS = frozenset(aliases.PACK_SIZE_UNITS)
_TIMES_RE = re.compile(r"^(?:x(\d{1,4})|(\d{1,4})x)$")
_GLUED_RE = re.compile(r"^(\d{1,4})(packets?|packs?|pkts?|pcs?|pieces?)$")


@dataclass(frozen=True)
class Quantity:
    value: int
    unit: str | None
    indices: frozenset[int]


def unit_for(token: str) -> str | None:
    for word, unit in _UNITS:
        if word_matches(token, word):
            return unit
    return None


def number_value(token: str) -> int | None:
    if token.isdecimal():
        return int(token) if len(token) <= MAX_DIGITS else None
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS[token]
    times = _TIMES_RE.match(token)
    if times:
        return int(times.group(1) or times.group(2))
    return None


def extract_quantity(
    tokens: list[str],
    *,
    skip: set[int] | frozenset[int] = frozenset(),
    is_entity_word: Callable[[str], bool] = lambda _word: False,
) -> Quantity | None:
    """First quantity in ``tokens``, ignoring indices in ``skip``."""
    for i, token in enumerate(tokens):
        if i in skip:
            continue
        glued = _GLUED_RE.match(token)
        if glued:
            return Quantity(int(glued.group(1)), unit_for(glued.group(2)), frozenset({i}))
        value = number_value(token)
        if value is None:
            continue
        following = tokens[i + 1] if i + 1 < len(tokens) and i + 1 not in skip else None
        if following in _PACK_SIZE_UNITS:
            continue  # "500 g" is a pack size
        unit = unit_for(following) if following else None
        if token in _AMBIGUOUS and unit is None and not (following and is_entity_word(following)):
            continue
        indices = {i, i + 1} if unit else {i}
        return Quantity(value, unit, frozenset(indices))
    return None


def parse_quantity(text: str) -> tuple[int | None, str | None]:
    """Convenience for callers that only need ``(quantity, unit)`` from free text."""
    from . import constraints  # local: constraints needs this module's unit_for

    tokens = tokenize(constraints.prepare(text or ""))
    found = extract_quantity(tokens, skip=constraints.price_indices(tokens))
    return (found.value, found.unit) if found else (None, None)

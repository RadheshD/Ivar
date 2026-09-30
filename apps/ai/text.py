"""Text normalization shared by every stage and by the keyword tables."""

import unicodedata

_NUKTA = "़"
_CHANDRABINDU = "ँ"
_ANUSVARA = "ं"
_APOSTROPHES = "'’"

# Indic keywords at least this long also match as a word prefix ("తెలుగు" in "తెలుగులో").
MIN_PREFIX_LENGTH = 3


def normalize(text: str) -> str:
    """Casefold, unify Devanagari spellings and replace punctuation/symbols with spaces.

    Letters, combining marks and digits of every script are kept, so Telugu and
    Devanagari words survive intact. Zero-width joiners become spaces, which splits
    Telugu suffixes written with ZWNJ ("కార్ట్‌లో" -> "కార్ట్ లో").
    """
    text = unicodedata.normalize("NFC", text).casefold()
    text = text.replace(_NUKTA, "").replace(_CHANDRABINDU, _ANUSVARA)
    chars = []
    for ch in text:
        if ch in _APOSTROPHES:
            continue
        chars.append(ch if unicodedata.category(ch)[0] in "LMN" else " ")
    return " ".join("".join(chars).split())


def tokenize(text: str) -> list[str]:
    return normalize(text).split()


def is_indic(word: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" or "ఀ" <= ch <= "౿" for ch in word)


def word_matches(token: str, keyword: str) -> bool:
    if token == keyword:
        return True
    return is_indic(keyword) and len(keyword) >= MIN_PREFIX_LENGTH and token.startswith(keyword)


def phrases(entries) -> tuple[tuple[str, ...], ...]:
    """Normalize table entries into word tuples, longest first."""
    result = {tuple(normalize(e).split()) for e in entries}
    result.discard(())
    return tuple(sorted(result, key=lambda p: (-len(p), p)))


def find(tokens: list[str], table: tuple[tuple[str, ...], ...]) -> set[int]:
    """Indices of every token covered by any phrase in ``table``."""
    hits: set[int] = set()
    for phrase in table:
        size = len(phrase)
        for start in range(len(tokens) - size + 1):
            if all(word_matches(tokens[start + k], phrase[k]) for k in range(size)):
                hits.update(range(start, start + size))
    return hits

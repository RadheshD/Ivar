"""Best-guess message language. The customer's saved language is never overridden here."""

from . import aliases
from .text import tokenize

_TELUGU_MARKERS = frozenset(aliases.TELUGU_MARKERS)
_HINDI_MARKERS = frozenset(aliases.HINDI_MARKERS)


def detect_language(text: str | None) -> str | None:
    """Return "te", "hi", "en" or ``None`` (nothing to go on, or a tie).

    Script decides first: Telugu script -> "te", Devanagari -> "hi". Romanized text is
    scored by Telugu and Hindi marker words; Latin text with no markers is "en".
    """
    if not text:
        return None
    telugu = sum("ఀ" <= ch <= "౿" for ch in text)
    devanagari = sum("ऀ" <= ch <= "ॿ" for ch in text)
    if telugu or devanagari:
        if telugu == devanagari:
            return None
        return "te" if telugu > devanagari else "hi"

    tokens = tokenize(text)
    te = sum(t in _TELUGU_MARKERS for t in tokens)
    hi = sum(t in _HINDI_MARKERS for t in tokens)
    if te or hi:
        if te == hi:
            return None
        return "te" if te > hi else "hi"
    if any(ch.isascii() and ch.isalpha() for ch in text):
        return "en"
    return None

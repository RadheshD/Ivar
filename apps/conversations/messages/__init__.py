"""Localized reply text. Telugu and Hindi need native-speaker review before launch."""

from apps.ai.text import normalize

from . import en, hi, te

CATALOGS = {"en": en.MESSAGES, "te": te.MESSAGES, "hi": hi.MESSAGES}
DEFAULT_LANGUAGE = "en"
NEEDS_NATIVE_REVIEW = ("te", "hi")

# Any language's words are accepted, whatever the saved language.
ESCAPE_WORDS = frozenset(normalize(w) for module in (en, te, hi) for w in module.ESCAPE_WORDS)
DIET_WORDS = {
    diet: frozenset(normalize(w) for module in (en, te, hi) for w in module.DIET_WORDS[diet])
    for diet in en.DIET_WORDS
}


def t(language: str | None, key: str, **values) -> str:
    """Text for ``key`` in ``language``; English when the language or key is missing."""
    catalog = CATALOGS.get(language or DEFAULT_LANGUAGE, en.MESSAGES)
    template = catalog.get(key) or en.MESSAGES[key]
    return template.format(**values) if values else template

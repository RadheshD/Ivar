"""Rules / keywords / aliases stage.

Detects intent signals in the normalized words, pulls out entities (quantity, choice,
order number, product/category text) and applies a fixed priority order. Returns raw
entity text only; nothing here knows real product IDs, prices or stock.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from . import aliases, constraints
from .intents import ORDER_INTENTS, Entities, Intent
from .quantities import extract_quantity, number_value, unit_for
from .text import find, normalize, phrases, word_matches

MAX_HINTS = 500
MAX_HINT_LENGTH = 100
MAX_ENTITY_LENGTH = 100
NAME_CUTOFF = 0.8  # difflib ratio for whole catalog names (fuzzy stage only)

STRONG = 0.9  # an explicit keyword decided
MEDIUM = 0.8  # decided without a keyword (e.g. bare product name, "2 makhana")
WEAK_WANT = 0.4  # "I want ..." with nothing recognisable
WEAK_ORDER = 0.5  # "order" / "status" on its own

_SIGNALS = {
    "greeting": phrases(aliases.GREETING),
    "lang_change": phrases(aliases.LANGUAGE_CHANGE),
    "track": phrases(aliases.TRACKING),
    "checkout": phrases(aliases.CHECKOUT),
    "payment": phrases(aliases.PAYMENT),
    "order_noun": phrases(aliases.ORDER_NOUN),
    "status": phrases(aliases.STATUS),
    "my": phrases(aliases.MY),
    "remove": phrases(aliases.REMOVE),
    "update_strong": phrases(aliases.UPDATE_STRONG),
    "update_weak": phrases(aliases.UPDATE_WEAK),
    "add": phrases(aliases.ADD),
    "cart": phrases(aliases.CART),
    "select": phrases(aliases.SELECT),
    "help": phrases(aliases.HELP),
    "menu": phrases(aliases.MENU),
    "want": phrases(aliases.WANT),
}
# Signals that name an intent of their own; a bare language name loses to any of them.
_INTENT_SIGNALS = (
    "track", "checkout", "payment", "order_noun", "remove", "update_strong", "add", "cart",
    "select", "help", "menu",
)  # fmt: skip

_LANGUAGE_NAMES = {code: phrases(words) for code, words in aliases.LANGUAGE_NAMES.items()}
_ORDINALS = {index: phrases(words) for index, words in aliases.ORDINALS.items()}
_CHOICE_PREFIX = frozenset(aliases.CHOICE_PREFIX)
_FILLERS = frozenset(normalize(w) for w in aliases.FILLERS)
_TERM_ALIASES = {normalize(k): v for k, v in aliases.TERM_ALIASES.items()}
_CATEGORY_TERMS = {key: phrases(words) for key, words in aliases.CATEGORY_TERMS.items()}
_CATEGORY_WORDS = frozenset(w for table in _CATEGORY_TERMS.values() for p in table for w in p)
_PRODUCT_WORDS = frozenset(aliases.PRODUCT_VOCAB)
_ORDER_TOKEN_RE = re.compile(r"^[a-z]{2,5}\d{3,12}$")
_LETTERS_RE = re.compile(r"^[a-z]{2,5}$")
_DIGITS_RE = re.compile(r"^\d{3,12}$")


def _table_words(*tables) -> frozenset[str]:
    return frozenset(w for table in tables for phrase in table for w in phrase)


# Every word some table knows. Unknown Latin words are fuzzy-correction candidates.
KNOWN_WORDS = (
    _table_words(*_SIGNALS.values(), *_LANGUAGE_NAMES.values(), *_ORDINALS.values())
    | _FILLERS
    | _CATEGORY_WORDS
    | _PRODUCT_WORDS
    | frozenset(_TERM_ALIASES)
    | frozenset(normalize(w) for w in aliases.NUMBER_WORDS)
    | frozenset(normalize(w) for words in aliases.UNITS.values() for w in words)
    | frozenset(aliases.PACK_SIZE_UNITS)
    | _CHOICE_PREFIX
)


@dataclass(frozen=True)
class Vocabulary:
    """Built-in words plus optional caller-supplied catalog names (plain strings)."""

    product_names: tuple[str, ...] = ()
    category_names: tuple[str, ...] = ()

    @classmethod
    def build(cls, product_names: Iterable = (), category_names: Iterable = ()) -> Vocabulary:
        return cls(_clean_hints(product_names), _clean_hints(category_names))

    @property
    def products_by_norm(self) -> dict[str, str]:
        return {normalize(n): n for n in self.product_names}

    @property
    def categories_by_norm(self) -> dict[str, str]:
        return {normalize(n): n for n in self.category_names}

    @property
    def hint_words(self) -> frozenset[str]:
        names = self.product_names + self.category_names
        return frozenset(w for n in names for w in normalize(n).split())

    def is_entity_word(self, word: str) -> bool:
        word = _TERM_ALIASES.get(word, word)
        return word in _PRODUCT_WORDS or word in _CATEGORY_WORDS or word in self.hint_words

    def knows(self, word: str) -> bool:
        return word in KNOWN_WORDS or word in self.hint_words


def _clean_hints(names: Iterable) -> tuple[str, ...]:
    if isinstance(names, str) or names is None:
        return ()
    cleaned: list[str] = []
    for name in names:
        if isinstance(name, str) and name.strip() and len(name) <= MAX_HINT_LENGTH:
            cleaned.append(name.strip())
        if len(cleaned) >= MAX_HINTS:
            break
    return tuple(dict.fromkeys(cleaned))


@dataclass(frozen=True)
class RuleMatch:
    intent: Intent | None  # None: nothing recognised
    confidence: float
    entities: Entities


def _language(tokens: list[str]) -> tuple[str | None, set[int]]:
    for code, table in _LANGUAGE_NAMES.items():
        hits = find(tokens, table)
        if hits:
            return code, hits
    return None, set()


def _order_number(
    tokens: list[str], vocab: Vocabulary, skip: frozenset[int] = frozenset()
) -> tuple[str | None, set[int]]:
    for i, token in enumerate(tokens):
        if i in skip or (i + 1 in skip and i + 1 < len(tokens)):
            continue  # a price word and its number are never an order number
        if _ORDER_TOKEN_RE.match(token):
            return token.upper(), {i}
        if (
            _LETTERS_RE.match(token)
            and not vocab.knows(token)
            and i + 1 < len(tokens)
            and _DIGITS_RE.match(tokens[i + 1])
        ):
            return (token + tokens[i + 1]).upper(), {i, i + 1}
    return None, set()


def _choice(tokens: list[str]) -> tuple[int | None, set[int]]:
    for index, table in _ORDINALS.items():
        hits = find(tokens, table)
        if hits:
            return index, hits
    for i, token in enumerate(tokens[:-1]):
        if token in _CHOICE_PREFIX:
            value = number_value(tokens[i + 1])
            if value is not None and value >= 1:
                return value, {i, i + 1}
    return None, set()


def _category_key(words: list[str]) -> str | None:
    """Canonical category key when ``words`` are exactly one built-in category term."""
    for key, table in _CATEGORY_TERMS.items():
        for phrase in table:
            if len(phrase) == len(words) and all(map(word_matches, words, phrase)):
                return key
    return None


def closest_name(phrase: str, names_by_norm: dict[str, str]) -> str | None:
    """Closest caller-supplied catalog name for a whole phrase (difflib), or ``None``."""
    if not phrase or not names_by_norm:
        return None
    close = difflib.get_close_matches(phrase, list(names_by_norm), n=1, cutoff=NAME_CUTOFF)
    return names_by_norm[close[0]] if close else None


def _entity(
    leftover: list[str], vocab: Vocabulary, fuzzy_hints: bool
) -> tuple[str | None, str | None, bool]:
    """Return ``(product_query, category_query, known)`` for the leftover words."""
    if not leftover:
        return None, None, False
    words = [_TERM_ALIASES.get(w, w) for w in leftover]
    phrase = " ".join(words)[:MAX_ENTITY_LENGTH]

    categories = vocab.categories_by_norm
    products = vocab.products_by_norm
    if phrase in categories:
        return None, categories[phrase], True
    if phrase in products:
        return products[phrase], None, True
    key = _category_key(words)
    if key:
        return None, key, True
    if fuzzy_hints:
        close = closest_name(phrase, categories)
        if close:
            return None, close, True
        close = closest_name(phrase, products)
        if close:
            return close, None, True
    known = any(vocab.is_entity_word(w) for w in words)
    return phrase, None, known


def match(
    tokens: list[str],
    vocab: Vocabulary,
    *,
    fuzzy_hints: bool = False,
    no_quantity: frozenset[int] = frozenset(),
) -> RuleMatch:
    """Classify normalized ``tokens``. Always returns entities, even with no intent.

    An order number is extracted only when the result is an order intent; otherwise the
    words are matched again with order-number extraction off, so a token such as
    "abc1234" stays ordinary text.
    """
    found = _match(tokens, vocab, fuzzy_hints, find_order_number=True, no_quantity=no_quantity)
    if found.entities.order_number and found.intent not in ORDER_INTENTS:
        found = _match(tokens, vocab, fuzzy_hints, find_order_number=False, no_quantity=no_quantity)
    return found


def _match(
    tokens: list[str],
    vocab: Vocabulary,
    fuzzy_hints: bool,
    *,
    find_order_number: bool,
    no_quantity: frozenset[int] = frozenset(),
) -> RuleMatch:
    signal = {name: find(tokens, table) for name, table in _SIGNALS.items()}
    language, language_hits = _language(tokens)
    # A number beside a price word or currency is a price, never a quantity (or an order
    # number): it is kept out of every entity below.
    price_hits = constraints.price_indices(tokens)
    if no_quantity and not (signal["add"] or signal["update_strong"] or signal["update_weak"]):
        # a hyphenated pair of numbers ("snacks 15-20") with no price cue and no explicit
        # add/update word is not one quantity
        price_hits = price_hits | no_quantity
    order_number, order_hits = (
        _order_number(tokens, vocab, price_hits) if find_order_number else (None, set())
    )
    choice, choice_hits = _choice(tokens)

    used: set[int] = set().union(
        *signal.values(), language_hits, order_hits, choice_hits, price_hits
    )
    quantity = extract_quantity(tokens, skip=used, is_entity_word=vocab.is_entity_word)
    if quantity:
        used |= quantity.indices
    leftover = [t for i, t in enumerate(tokens) if i not in used and t not in _FILLERS]
    leftover = [t for t in leftover if not unit_for(t)]
    product_query, category_query, known = _entity(leftover, vocab, fuzzy_hints)

    entities = Entities(
        product_query=product_query,
        category_query=category_query,
        quantity=quantity.value if quantity else None,
        unit=quantity.unit if quantity else None,
        language_choice=language,
        order_number=order_number,
        choice_index=choice,
    )
    has = {name: bool(hits) for name, hits in signal.items()}
    has_entity = bool(product_query or category_query)

    def decided(intent: Intent, confidence: float = STRONG) -> RuleMatch:
        return RuleMatch(intent, confidence, entities)

    if has["lang_change"]:
        return decided(Intent.LANGUAGE_SELECT)
    if language and not has_entity and not any(has[s] for s in _INTENT_SIGNALS):
        return decided(Intent.LANGUAGE_SELECT)
    if has["track"]:
        return decided(Intent.TRACKING)
    if has["checkout"]:
        return decided(Intent.CHECKOUT)
    if has["payment"]:
        return decided(Intent.PAYMENT_STATUS)
    if has["order_noun"] and (has["status"] or has["my"] or order_number):
        return decided(Intent.ORDER_STATUS)
    other_intent = has["want"] or any(has[s] for s in _INTENT_SIGNALS)
    if order_number and not has_entity and not other_intent:
        return decided(Intent.ORDER_STATUS, MEDIUM)
    if has["remove"]:
        return decided(Intent.REMOVE_FROM_CART)
    if has["update_strong"] or (has["update_weak"] and quantity and not has["add"]):
        return decided(Intent.UPDATE_QUANTITY)
    if has["add"]:
        return decided(Intent.ADD_TO_CART)
    if quantity and has_entity:
        return decided(Intent.ADD_TO_CART, MEDIUM)
    if has["cart"]:
        return decided(Intent.VIEW_CART)
    if has["select"] or choice:
        return decided(Intent.PRODUCT_SELECT)
    if has_entity and (has["want"] or known):
        intent = Intent.CATEGORY_SELECT if not product_query else Intent.PRODUCT_SEARCH
        return decided(intent, STRONG if has["want"] else MEDIUM)
    if has["help"]:
        return decided(Intent.HELP)
    if has["menu"]:
        return decided(Intent.CATEGORY_SELECT)
    if has["greeting"]:
        return decided(Intent.GREETING)

    # Low-confidence candidates: never acted on without clarification.
    if has["want"]:
        return decided(Intent.PRODUCT_SEARCH, WEAK_WANT)
    if has["order_noun"] or has["status"]:
        return decided(Intent.ORDER_STATUS, WEAK_ORDER)
    return RuleMatch(None, 0.0, entities)

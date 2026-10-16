"""Constraint detection: price limits, diet, high protein / low calorie. Code only, no model.

Two jobs, both driven by the data in ``constraint_cues.py``:

1. A number that sits next to a price word or a currency word is a PRICE, never a quantity.
   ``Constraints.price_indices`` names those tokens so the rules layer can keep them out of
   quantity extraction on every path (search, add, fuzzy, pending choice, taps).
2. The constraints themselves (``max_price``, ``min_price``, ``diet_type``, ``sort``) are read
   in code so the recommendation flow can apply them strictly, whatever a model returned.

Nothing here logs. ``Constraints.names`` holds constraint NAMES only (never values or text),
for the one-line-per-message log.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from . import constraint_cues as cc
from .aliases import PACK_SIZE_UNITS
from .intents import RecommendRequest
from .quantities import unit_for
from .recommend import constraint_keys
from .text import normalize, phrases, word_matches

MAX_DIGITS = 6
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_GLUED_CURRENCY_RE = re.compile(r"^(rs|inr)(\d{1,6})$")
_PACK_UNITS = frozenset(PACK_SIZE_UNITS)

PRICE = "price"  # an upper limit only (the name the first release logged)
PRICE_MIN = "price_min"
PRICE_RANGE = "price_range"
DIET = "diet"
PROTEIN_SORT = "protein_sort"
CALORIE_SORT = "calorie_sort"


def _flatten(table: cc.Table) -> tuple[tuple[str, ...], ...]:
    return phrases([w for _review, words in table.values() for w in words])


_CURRENCY = _flatten(cc.CURRENCY)
_MAX_BEFORE = _flatten(cc.PRICE_MAX_BEFORE)
_MAX_AFTER = _flatten(cc.PRICE_MAX_AFTER)
_MIN_BEFORE = _flatten(cc.PRICE_MIN_BEFORE)
_MIN_AFTER = _flatten(cc.PRICE_MIN_AFTER)
_BETWEEN = _flatten(cc.PRICE_BETWEEN)
_DIETS = tuple((diet, _flatten(table)) for diet, table in cc.DIET.items())
_HIGH_PROTEIN = _flatten(cc.HIGH_PROTEIN)
_LOW_CALORIE = _flatten(cc.LOW_CALORIE)
_BARE_PROTEIN = _flatten(cc.BARE_PROTEIN)
_RANGE_JOIN = _flatten(cc.PRICE_RANGE_JOIN)
_SPELLED = {
    normalize(word): value
    for _review, words in cc.SPELLED_VALUES.values()
    for word, value in words.items()
}
_HUNDRED = frozenset(normalize(w) for _r, ws in cc.SPELLED_HUNDRED.values() for w in ws)
_THOUSAND = frozenset(normalize(w) for _r, ws in cc.SPELLED_THOUSAND.values() for w in ws)
# Comparators that unmistakably announce a price limit. Common words ("max", "budget", "over")
# are left out: they alone do not prove the customer meant a price.
_UNAMBIGUOUS_MAX = phrases(
    [
        "under", "below", "less than", "lesser than", "within", "upto", "up to", "at most",
        "not more than", "no more than", "cheaper than",
    ]
)  # fmt: skip
_FRACTIONS = {
    tuple(normalize(phrase).split()): value
    for _review, table in cc.SPELLED_FRACTIONS.values()
    for phrase, value in table.items()
}
RANGE_SENTINEL = "rngto"
_HYPHEN_RANGE_RE = re.compile(r"(?<=\d)\s*[-\u2013\u2014]\s*(?=(?:rs\.?\s*|inr\s*)?\d)")


@dataclass(frozen=True)
class Constraints:
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    diet_type: str | None = None
    sort: str | None = None
    # Token indices (of the tokens given to ``parse``) of every number and currency word that
    # belongs to a price. These are never a quantity.
    price_indices: frozenset[int] = frozenset()
    price_values: frozenset[int] = frozenset()  # the numbers themselves
    has_range: bool = False  # a "N to M" / "N-M" pair was read as a price range
    # A price comparator or currency word was found but no number could be read from it.
    unreadable: bool = False
    # Every token that is part of a constraint (price, comparator, diet, protein/calorie
    # word): the fuzzy stage must leave these exactly as typed.
    cue_indices: frozenset[int] = frozenset()
    comparator_words: frozenset[str] = frozenset()  # "under", "less", "than", ...
    diet_words: frozenset[str] = frozenset()
    names: tuple[str, ...] = ()  # constraint NAMES that call for the recommendation flow

    def __bool__(self) -> bool:
        return bool(self.names)


def prepare(text: str) -> str:
    """Text as the tokenizer should see it: the rupee sign becomes the word "rs" (it would
    otherwise vanish as a symbol) and "1,000" becomes "1000"."""
    text = unicodedata.normalize("NFKC", text)
    return _THOUSANDS_RE.sub("", text.replace("₹", " rs "))


def prepare_ranges(text: str) -> str:
    """``prepare`` plus: a hyphen between two numbers becomes the word "rngto". The caller
    keeps the result only when ``parse`` reads a price range from it, so "add 2-3 quinoa" and
    other pairs with no price cue are tokenized exactly as before."""
    return _HYPHEN_RANGE_RE.sub(f" {RANGE_SENTINEL} ", prepare(text))


def has_hyphen_range(text: str) -> bool:
    return bool(_HYPHEN_RANGE_RE.search(prepare(text)))


def hyphen_pair_indices(text: str) -> frozenset[int]:
    """Token indices (in the ordinary tokenization) of the numbers around each hyphen between
    two numbers; used to keep such a pair out of quantity extraction when it is not a range."""
    ranged = normalize(prepare_ranges(text)).split()
    pairs: set[int] = set()
    removed = 0
    for i, token in enumerate(ranged):
        if token == RANGE_SENTINEL:
            pairs.update({i - 1 - removed, i - removed})
            removed += 1
    return frozenset(p for p in pairs if p >= 0)


def _split(tokens: Sequence[str]) -> list[tuple[str, int]]:
    """Sub-tokens with the index of the token they came from: "300లోపు" -> "300", "లోపు";
    "15g" -> "15", "g"; "rs300" -> "rs", "300"."""
    out: list[tuple[str, int]] = []
    for i, token in enumerate(tokens):
        n = 0
        while n < len(token) and token[n].isdecimal():
            n += 1
        glued = _GLUED_CURRENCY_RE.match(token)
        if 0 < n < len(token):
            out += [(token[:n], i), (token[n:], i)]
        elif glued:
            out += [(glued.group(1), i), (glued.group(2), i)]
        else:
            out.append((token, i))
    return out


def _number(word: str) -> int | None:
    if word.isdecimal() and len(word) <= MAX_DIGITS:
        return int(word) or None
    return None


def _matches_at(words: list[str], start: int, phrase: tuple[str, ...]) -> bool:
    end = start + len(phrase)
    return (
        start >= 0
        and end <= len(words)
        and all(word_matches(words[start + j], phrase[j]) for j in range(len(phrase)))
    )


def _phrase_at(words, start, table) -> tuple[str, ...] | None:
    return next((p for p in table if _matches_at(words, start, p)), None)


def _phrase_ending_at(words, end, table) -> tuple[str, ...] | None:
    return next((p for p in table if _matches_at(words, end - len(p) + 1, p)), None)


def _is_currency(word: str) -> bool:
    return any(len(p) == 1 and word_matches(word, p[0]) for p in _CURRENCY)


def _find_all(words: list[str], table, covered: set[int]) -> list[tuple[int, ...]]:
    """Index tuples of every phrase occurrence not touching ``covered`` (which it extends)."""
    found = []
    for start in range(len(words)):
        for p in table:
            span = tuple(range(start, start + len(p)))
            if _matches_at(words, start, p) and not covered.intersection(span):
                found.append(span)
                covered.update(span)
                break
    return found


def _is_indic(word: str) -> bool:
    return any(ord(ch) > 0x900 for ch in word)


def _value_of(word: str) -> int | None:
    value = _SPELLED.get(word)
    if value is None and _is_indic(word):
        value = next(
            (v for k, v in _SPELLED.items() if len(k) >= 3 and word_matches(word, k)), None
        )
    return value


def _is_mult(word: str, table: frozenset[str]) -> bool:
    return word in table or (
        _is_indic(word) and any(len(k) >= 3 and word_matches(word, k) for k in table)
    )


def _spelled(words: list[str], k: int) -> tuple[int, int, list[int]] | None:
    """A spelled-out number starting at ``k``: ``(value, length, part values)`` or ``None``.
    English "two hundred", "two fifty", "three hundred and fifty", "one thousand"; Hindi and
    Telugu words use the same grammar. A run ends at the first word that does not continue it."""
    n = len(words)
    j = k
    total = group = 0
    last = "start"  # start | ones | tens | teen | hund | thou
    parts: list[int] = []
    if (
        words[j] == "a"
        and j + 1 < n
        and (_is_mult(words[j + 1], _HUNDRED) or _is_mult(words[j + 1], _THOUSAND))
    ):
        group, last, j = 1, "ones", j + 1
    while j < n:
        w = words[j]
        value = _value_of(w)
        if value is not None:
            kind = "ones" if value < 10 else "teen" if value < 20 else "tens"
            if kind == "ones" and last in ("start", "hund", "thou", "tens"):
                if last == "tens" and group % 10:
                    break
                group += value
            elif kind == "tens" and last == "ones" and 1 <= group <= 9 and not total:
                group = group * 100 + value  # "two fifty"
            elif kind in ("tens", "teen") and last in ("start", "hund", "thou"):
                group += value
            else:
                break
            last = kind
            parts.append(value)
        elif _is_mult(w, _HUNDRED) and last in ("start", "ones") and group < 10:
            group, last = (group or 1) * 100, "hund"
            parts.append(100)
        elif _is_mult(w, _THOUSAND) and last in ("start", "ones", "tens", "teen", "hund"):
            total, group, last = total + (group or 1) * 1000, 0, "thou"
            parts.append(1000)
        elif (
            w == "and"
            and last in ("hund", "thou")
            and j + 1 < n
            and _value_of(words[j + 1])
            and not (
                j + 2 < n
                and (_is_mult(words[j + 2], _HUNDRED) or _is_mult(words[j + 2], _THOUSAND))
            )
        ):
            pass  # "three hundred and fifty" (but not "two hundred and three hundred")
        else:
            break
        j += 1
    value = total + group
    if j == k or not 0 < value <= 999_999:
        return None
    return value, j - k, parts


def _numbers(words: list[str]) -> tuple[dict[int, tuple[int, int]], set[int]]:
    """Numbers by start index -> ``(value, length)``, plus the part values of spelled ones.
    Digit runs always count; a spelled-out one is only USED where a price cue is beside it."""
    found: dict[int, tuple[int, int]] = {}
    parts: set[int] = set()
    k = 0
    while k < len(words):
        digits = _number(words[k])
        if digits is not None:
            found[k] = (digits, 1)
            k += 1
            continue
        fraction = next(
            ((len(p), v) for p, v in _FRACTIONS.items() if _matches_at(words, k, p)), None
        )
        if fraction:
            found[k] = (fraction[1], fraction[0])
            parts.add(fraction[1])
            k += fraction[0]
            continue
        run = _spelled(words, k)
        if run:
            found[k] = (run[0], run[1])
            parts.update(run[2])
            k += run[1]
        else:
            k += 1
    return found, parts


def _anywhere(words: list[str], *tables) -> bool:
    return any(_phrase_at(words, i, t) for i in range(len(words)) for t in tables)


def parse(tokens: Sequence[str]) -> Constraints:
    """Read the constraints out of normalized ``tokens`` (see ``prepare``)."""
    sub = _split(tokens)
    words = [w for w, _ in sub]
    origin = [i for _, i in sub]
    price_at: set[int] = set()  # indices into ``words``
    comparator: set[int] = set()
    values: set[int] = set()
    maxes: list[int] = []
    mins: list[int] = []
    ranged = False
    nums, parts = _numbers(words)
    diet_cover: set[int] = set()
    diets_all = [(d, _find_all(words, t, diet_cover)) for d, t in _DIETS]
    protein_spans = _find_all(words, _HIGH_PROTEIN, set())
    calorie_spans = _find_all(words, _LOW_CALORIE, set())
    # A price or constraint cue anywhere in the message: a bare "N to M" needs one.
    cue_anywhere = (
        any(_is_currency(w) for w in words)
        or _anywhere(words, _MAX_BEFORE, _MAX_AFTER, _MIN_BEFORE, _MIN_AFTER)
        or any(spans for _d, spans in diets_all)
        or bool(protein_spans or calorie_spans)
    )

    def is_cur(index: int) -> bool:
        return 0 <= index < len(words) and _is_currency(words[index])

    for k in sorted(nums):
        value, length = nums[k]
        if k in price_at:
            continue
        end = k + length - 1
        cur_before = is_cur(k - 1)
        cur_after = is_cur(end + 1)
        before_end = k - 2 if cur_before else k - 1
        after_start = end + 2 if cur_after else end + 1
        following = words[end + 1] if end + 1 < len(words) else ""
        if (following in _PACK_UNITS or unit_for(following)) and not cur_after:
            continue  # "500 g" is a pack size and "2 packets" a quantity, not a price

        # ranges: "between 250 and 300", "250 to 300", "250 se 300", "250 nundi 300"
        between = _phrase_ending_at(words, before_end, _BETWEEN)
        joiner = _phrase_at(words, after_start, _RANGE_JOIN)
        if between and words[after_start : after_start + 1] == ["and"]:
            joiner = ("and",)
        if joiner and (between or cue_anywhere):
            second = after_start + len(joiner)
            cur2_before = is_cur(second)
            first2 = second + 1 if cur2_before else second
            if first2 in nums:
                value2, length2 = nums[first2]
                end2 = first2 + length2 - 1
                cur2_after = is_cur(end2 + 1)
                tail = end2 + (2 if cur2_after else 1)
                price_at.update(range(k, end + 1))
                price_at.update(range(first2, end2 + 1))
                price_at.update(range(after_start, second))  # the joiner word(s)
                for flag, at in (
                    (cur_before, k - 1),
                    (cur_after, end + 1),
                    (cur2_before, second),
                    (cur2_after, end2 + 1),
                ):
                    if flag:
                        price_at.add(at)
                if between:
                    comparator.update(range(before_end - len(between) + 1, before_end + 1))
                tail_hit = _phrase_at(words, tail, _MAX_AFTER)
                if tail_hit:
                    comparator.update(range(tail, tail + len(tail_hit)))
                values.update({value, value2})
                mins.append(min(value, value2))
                maxes.append(max(value, value2))
                ranged = True
                continue

        kind = None
        for table, which in ((_MAX_BEFORE, "max"), (_MIN_BEFORE, "min")):
            hit = _phrase_ending_at(words, before_end, table)
            if hit and (which == "max" or cur_before or cur_after):
                kind = which
                comparator.update(range(before_end - len(hit) + 1, before_end + 1))
                break
        if kind is None:
            for table, which in ((_MAX_AFTER, "max"), (_MIN_AFTER, "min")):
                hit = _phrase_at(words, after_start, table)
                if hit and (which == "max" or cur_before or cur_after):
                    kind = which
                    comparator.update(range(after_start, after_start + len(hit)))
                    break
        if kind is None and not (cur_before or cur_after):
            continue
        price_at.update(range(k, end + 1))
        values.add(value)
        if cur_before:
            price_at.add(k - 1)
        if cur_after:
            price_at.add(end + 1)
        if kind == "max":
            maxes.append(value)
        elif kind == "min":
            mins.append(value)

    if mins and maxes and max(mins) > min(maxes):  # a reversed pair: swap, never ignore
        low, high = min(*mins, *maxes), max(*mins, *maxes)
        mins, maxes = [low], [high]

    diets = {d: spans for d, spans in diets_all if spans}
    diet_words = {words[i] for spans in diets.values() for span in spans for i in span}
    diet = next(iter(diets)) if len(diets) == 1 else None

    if (maxes or mins) and not protein_spans:
        # the bare word "protein" beside a price is the protein-sort cue
        protein_spans = _find_all(words, _BARE_PROTEIN, set())
    high_protein, low_calorie = bool(protein_spans), bool(calorie_spans)
    sort = None
    if high_protein != low_calorie:
        sort = "protein_desc" if high_protein else "calories_asc"

    names = []
    if mins and maxes:
        names.append(PRICE_RANGE)
    elif mins:
        names.append(PRICE_MIN)
    elif maxes:
        names.append(PRICE)
    if diet is not None:
        names.append(DIET)
    if high_protein:
        names.append(PROTEIN_SORT)
    if low_calorie:
        names.append(CALORIE_SORT)

    # A price word was written but no number could be read from it.
    unreadable = bool(
        not values
        and not nums
        and (any(_is_currency(w) for w in words) or _anywhere(words, _UNAMBIGUOUS_MAX))
    )

    cue_at = {*price_at, *comparator}
    cue_at.update(i for spans in diets.values() for span in spans for i in span)
    cue_at.update(i for span in (*protein_spans, *calorie_spans) for i in span)
    return Constraints(
        max_price=Decimal(min(maxes)) if maxes else None,
        min_price=Decimal(max(mins)) if mins else None,
        diet_type=diet,
        sort=sort,
        price_indices=frozenset(origin[i] for i in price_at),
        price_values=frozenset(values | (parts if price_at else set())),
        has_range=ranged,
        unreadable=unreadable,
        cue_indices=frozenset(origin[i] for i in cue_at),
        comparator_words=frozenset(words[i] for i in comparator),
        diet_words=frozenset(diet_words),
        names=tuple(names),
    )


def price_indices(tokens: Sequence[str]) -> frozenset[int]:
    """Indices of the tokens that are a price number or a currency word beside one."""
    return parse(tokens).price_indices


def _is_value(token: str, value: int) -> bool:
    return token.isdecimal() and len(token) <= MAX_DIGITS and int(token) == value


def drop_price_quantity(quantity: int | None, tokens: Sequence[str]) -> int | None:
    """``quantity`` from a model reply, or ``None`` when it is really a price in the message
    (a number the message uses only as a price)."""
    if quantity is None:
        return None
    cons = parse(tokens)
    if quantity not in cons.price_values:
        return quantity
    as_price = sum(1 for i in cons.price_indices if _is_value(tokens[i], quantity))
    plain = sum(1 for t in tokens if _is_value(t, quantity))
    return None if plain <= as_price else quantity


def overlay(request: RecommendRequest, cons: Constraints) -> RecommendRequest:
    """``request`` with the constraints read in code applied over it. A price or diet found
    in the message wins over whatever a model returned; a model's sort is kept."""
    from_code_sort = cons.sort is not None and request.sort != cons.sort
    code_keys = tuple(
        k
        for k, v in (
            ("max_price", cons.max_price),
            ("min_price", cons.min_price),
            ("diet_type", cons.diet_type),
            ("sort", cons.sort),
        )
        if v is not None
    )
    low = cons.min_price if cons.min_price is not None else request.min_price
    high = cons.max_price if cons.max_price is not None else request.max_price
    if low is not None and high is not None and low > high:
        low, high = high, low  # reversed bounds: swap them, never ignore them
    return replace(
        request,
        max_price=high,
        min_price=low,
        diet_type=cons.diet_type or request.diet_type,
        sort=cons.sort or request.sort,  # the code-read sort is never replaced by the model's
        sort_from_goal=request.sort_from_goal or from_code_sort,
        cues=cons.names,
        code_keys=code_keys,
        model_keys=tuple(k for k in constraint_keys(request) if k not in code_keys),
    )


def category_slugs_for_terms(tokens: Sequence[str], categories) -> tuple[str, ...]:
    """Slugs of the Port categories whose name holds a built-in category word that is in the
    message ("snacks" -> every category with "snacks" in its name). A bare "protein" or any
    other product word never selects a category."""
    from . import aliases

    words = set(tokens)
    found: list[str] = []
    for variants in aliases.CATEGORY_TERMS.values():
        used = [normalize(v) for v in variants if all(p in words for p in normalize(v).split())]
        if not used:
            continue
        for slug, name in categories:
            name_words = {w.rstrip("s") for w in normalize(name).split()}
            if slug not in found and any(
                all(p.rstrip("s") in name_words for p in term.split()) for term in used
            ):
                found.append(slug)
    return tuple(found)


def category_slug_in(tokens: Sequence[str], categories) -> str | None:
    """Slug of the longest category (``(slug, name)`` pairs) whose every word is in the
    message, or ``None``."""
    words = set(tokens)
    best: tuple[int, str] | None = None
    for slug, name in categories:
        parts = normalize(name).split()
        if parts and all(p in words for p in parts) and (best is None or len(parts) > best[0]):
            best = (len(parts), slug)
    return best[1] if best else None

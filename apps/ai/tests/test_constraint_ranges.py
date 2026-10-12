"""Price ranges, spelled-out prices, unreadable prices and the advice cue gate. Pure functions."""

from decimal import Decimal

import pytest

from apps.ai import constraints
from apps.ai.constraints import parse, prepare, prepare_ranges
from apps.ai.pipeline import IntentPipeline
from apps.ai.recommend import advice_cue_matches
from apps.ai.text import tokenize

NAMES = ["Protein Smoothie", "Quinoa", "Makhana Crunch"]


def understand(text):
    return IntentPipeline().understand(text, product_names=NAMES, category_names=["Snacks"])


def cons(text):
    """Constraints as the pipeline reads them (a hyphen range keeps its joiner word)."""
    if constraints.has_hyphen_range(text):
        ranged = parse(tokenize(prepare_ranges(text)))
        if ranged.has_range:
            return ranged
    return parse(tokenize(prepare(text)))


def bounds(text):
    c = cons(text)
    return c.min_price, c.max_price


D = Decimal


@pytest.mark.parametrize(
    "text",
    [
        "High protein 250-300",
        "250-300 high protein",
        "250 - 300 veg",
        "250 to 300 high protein",
        "between 250 and 300",
        "between Rs 250 and Rs 300",
        "Rs 250-300",
        "rs. 250 - 300",
        "₹250 to ₹300",
        "₹250-₹300",
        "250 se 300 tak",
        "250 se 300 rupaye",
        "250 nundi 300 varaku",
        "250 to 300 rupees",
        "250 – 300 snacks under",  # en dash
        "300-250 rupees",  # reversed: swapped
        "300 to 250 veg",
        "250 से 300 तक",
        "250 నుండి 300 వరకు",
    ],
)
def test_range_forms(text):
    assert bounds(text) == (D(250), D(300)), text
    assert "price_range" in cons(text).names


def test_range_numbers_are_never_quantities():
    for text in ("High protein 250-300", "250 to 300 high protein", "Rs 250-300 snacks"):
        r = understand(text)
        assert r.entities.quantity is None, text
        assert constraints.parse(tokenize(prepare_ranges(text))).price_indices


@pytest.mark.parametrize(
    "text, expected",
    [
        ("under two hundred", (None, D(200))),
        ("Something under two hundred", (None, D(200))),
        ("below two fifty", (None, D(250))),
        ("under one thousand", (None, D(1000))),
        ("under three hundred and fifty", (None, D(350))),
        ("under a hundred", (None, D(100))),
        ("under one hundred rupees", (None, D(100))),
        ("within five hundred", (None, D(500))),
        ("under two thousand five hundred", (None, D(2500))),
        ("two hundred rupees or less", (None, D(200))),
        ("above two hundred rupees", (D(200), None)),
        ("between two hundred and three hundred", (D(200), D(300))),
        ("under do sau", (None, D(200))),
        ("paanch sau ke andar", (None, D(500))),
        ("rendu vandalu lopu", (None, D(200))),
        ("veyyi lopu", (None, D(1000))),
        ("under दो सौ", (None, D(200))),
        ("दो सौ के अंदर", (None, D(200))),
        ("రెండు వందలు లోపు", (None, D(200))),
        ("వెయ్యి లోపు", (None, D(1000))),
    ],
)
def test_spelled_out_prices(text, expected):
    assert bounds(text) == expected, text
    assert understand(text).entities.quantity is None, text


def test_a_spelled_number_outside_a_price_context_is_still_a_quantity():
    assert understand("add two quinoa").entities.quantity == 2
    assert understand("two makhana crunch").entities.quantity == 2
    assert not cons("add two quinoa").names
    assert understand("add five quinoa").entities.quantity == 5


@pytest.mark.parametrize(
    "text", ["snacks 15-20", "protein 10-20", "oats 5-6", "makhana 3-4 please"]
)
def test_a_hyphen_pair_without_a_cue_is_not_a_range_and_not_a_quantity(text):
    assert bounds(text) == (None, None) and not cons(text).names
    assert understand(text).entities.quantity is None, text


def test_a_hyphen_pair_with_an_explicit_add_is_unchanged():
    r = understand("add 2-3 quinoa")
    assert r.intent.value == "ADD_TO_CART" and r.entities.quantity == 2
    assert r.entities.product_query == "3 quinoa"  # exactly what it was before this change
    assert bounds("add 2-3 quinoa") == (None, None)
    assert understand("2 to 3 quinoa").entities.quantity == 2
    assert understand("2 se 3 quinoa").entities.quantity == 2


def test_reversed_bounds_are_swapped_never_dropped():
    assert bounds("above Rs 300 under 250") == (D(250), D(300))
    assert bounds("under 250 above Rs 300") == (D(250), D(300))


def test_names_never_carry_values():
    assert cons("250-300 high protein").names == ("price_range", "protein_sort")
    assert cons("above Rs 200").names == ("price_min",)
    assert cons("under 300").names == ("price",)  # the upper-limit name kept from release 1


def test_min_only_and_max_only_bounds():
    assert bounds("above Rs 200 high protein") == (D(200), None)
    assert bounds("200 rupees or more") == (D(200), None)
    assert bounds("under 300") == (None, D(300))


@pytest.mark.parametrize(
    "text",
    ["something under", "under rupees", "within rs", "snacks below"],
)
def test_a_price_word_with_no_readable_number_is_flagged(text):
    assert cons(text).unreadable, text


@pytest.mark.parametrize(
    "text",
    ["under 300", "15 rupees", "add 2 quinoa", "snacks", "something under 2 packets",
     "snacks under two hundred", "show my cart"],
)  # fmt: skip
def test_readable_or_price_free_messages_are_not_flagged(text):
    assert not cons(text).unreadable, text


@pytest.mark.parametrize(
    "text",
    [
        "what should I eat",
        "What food do I need to take if I go to the gym regularly",
        "what do I need for muscle gain",
        "is it good for me",
        "Is this healthy?",
        "how much should I eat",
        "what's good for energy",
        "मुझे क्या खाना चाहिए",
        "నేను ఏమి తినాలి",
        "mujhe kya khana chahiye",
        "nenu emi tinali",
    ],
)
def test_advice_cues_match(text):
    assert advice_cue_matches(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Something high protein under 300",
        "300 ke andar high protein kuch",
        "suggest something",
        "recommend a snack",
        "high protein 250-300",
        "show me veg snacks",
    ],
)
def test_plain_requests_are_not_advice_cues(text):
    assert not advice_cue_matches(text), text

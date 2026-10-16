"""Constraint detection: a price number is never a quantity; price/diet/protein cues are read
in code. Pure functions, no network."""

from decimal import Decimal

import pytest

from apps.ai import constraints, rules
from apps.ai.constraints import parse, prepare
from apps.ai.pipeline import IntentPipeline
from apps.ai.quantities import parse_quantity
from apps.ai.text import tokenize

NAMES = [
    "Protein Smoothie",
    "High-Protein Paneer Bowl",
    "Plant Protein Mix",
    "Active Protein",
    "Date & Chocolate Protein Bites",
    "Quinoa",
    "Makhana Crunch",
]
CATEGORIES = ["Nuts, Seeds & Snacks"]


def understand(text):
    return IntentPipeline().understand(text, product_names=NAMES, category_names=CATEGORIES)


def cons(text):
    return parse(tokenize(prepare(text)))


PRICE_TEXTS = [
    "Something high protein under 300",
    "high protein under 15",
    "snacks under 20",
    "veg under 10",
    "below Rs 15",
    "below rs. 15",
    "15 rupees",
    "₹300 snacks",
    "under ₹300",
    "snacks 300 rupees",
    "less than 300",
    "within 300",
    "up to 300",
    "max 300",
    "budget 300",
    "300 INR",
    "under rs300",
    "under 1,000 rupees",
    "300 ke andar snacks",
    "300 se kam veg snacks",
    "300 tak",
    "300 లోపు స్నాక్స్",
    "300 కంటే తక్కువ",
    "300 వరకు స్నాక్స్",
    "300 lopu snacks",
    "300 के अंदर स्नैक्स",
    "300 से कम",
    "₹300 तक",
    "300 రూపాయలు",
    "300 रुपये",
]


@pytest.mark.parametrize("text", PRICE_TEXTS)
def test_a_price_number_is_never_a_quantity(text):
    result = understand(text)
    assert result.entities.quantity is None, text
    assert parse_quantity(text)[0] is None, text


@pytest.mark.parametrize("text", PRICE_TEXTS)
def test_the_number_is_marked_as_a_price(text):
    c = cons(text)
    tokens = tokenize(prepare(text))
    assert c.price_values, text
    assert any(any(ch.isdecimal() for ch in tokens[i]) for i in c.price_indices), text


def test_the_price_number_is_not_left_in_the_search_words(settings):
    # This pins the rules stage's entity extraction, so the code-only recommendation routes (which
    # answer these messages first by default) are switched off here.
    settings.IVA_CODE_CONSTRAINTS_ENABLED = False
    q = understand("Something high protein under 300").entities.product_query
    assert q == "something high protein under"
    assert understand("snacks under ₹20").entities.product_query == "snacks under"


def test_real_quantities_are_unchanged():
    for text, qty in [
        ("add 2 quinoa", 2),
        ("add 2 makhana crunch", 2),
        ("ad 2 makhana crunch", 2),
        ("at least 3 packets makhana", 3),
        ("2 makhana crunch", 2),
        ("add 5 quinoa under 300", 5),
    ]:
        assert understand(text).entities.quantity == qty, text


def test_results_without_price_words_are_identical_to_the_old_code(monkeypatch):
    texts = ["add 2 quinoa", "ad 2 makhana crunch", "protein smoothie", "show my cart", "3 quinoa"]
    new = [understand(t).to_dict() for t in texts]
    monkeypatch.setattr(rules.constraints, "price_indices", lambda tokens: frozenset())
    assert new == [understand(t).to_dict() for t in texts]


def test_a_pack_size_is_not_a_price():
    for text in ("snacks under 500 g", "oats under 1 kg", "makhana under 2 packets"):
        assert not cons(text).names, text


def test_a_min_comparator_needs_a_currency_word():
    assert cons("at least 3 packets makhana").min_price is None
    assert cons("above 3 snacks").min_price is None
    assert cons("above Rs 200").min_price == Decimal(200)
    assert cons("200 rupees or more").min_price == Decimal(200)


def test_max_and_min_values():
    assert cons("under 300").max_price == Decimal(300)
    assert cons("under Rs 300").max_price == Decimal(300)
    assert cons("under 300 rupees").max_price == Decimal(300)
    assert cons("₹300 or less").max_price == Decimal(300)
    assert cons("between 100 and 300 rupees") == cons("between 100 and 300 rupees")
    c = cons("between 100 and 300 rupees")
    assert (c.min_price, c.max_price) == (Decimal(100), Decimal(300))
    assert cons("15 rupees").names == ()  # an amount with no limit word is not a constraint


@pytest.mark.parametrize(
    "text, expected",
    [
        ("under 300 veg snacks", ("price", "diet")),
        ("300 se kam veg snacks", ("price", "diet")),
        ("300 ke andar high protein", ("price", "protein_sort")),
        ("300 lopu high protein", ("price", "protein_sort")),
        ("250 lopala veg snacks", ("price", "diet")),
        ("300 के अंदर हाई प्रोटीन", ("price", "protein_sort")),
        ("200 से कम शाकाहारी", ("price", "diet")),
        ("300 లోపు హై ప్రోటీన్", ("price", "protein_sort")),
        ("200 లోపు వెజ్ స్నాక్స్", ("price", "diet")),
        ("less than 300 rupees low calorie", ("price", "calorie_sort")),
        ("high protein", ("protein_sort",)),
        ("non-veg", ("diet",)),
        ("snacks", ()),
        ("protein bars", ()),
        ("add 2 quinoa", ()),
    ],
)
def test_constraint_names(text, expected):
    assert cons(text).names == expected


def test_diets_and_sorts():
    assert cons("non-veg under 300").diet_type == "non_veg"
    assert cons("veg snacks").diet_type == "veg"
    assert cons("egg bites").diet_type == "egg"
    assert cons("veg or egg").diet_type is None
    assert cons("high protein").sort == "protein_desc"
    assert cons("low calorie").sort == "calories_asc"
    assert cons("high protein low calorie").sort is None


def test_the_names_carry_no_values_or_customer_text():
    c = cons("zebrafoo high protein under 777")
    assert all(n in ("price", "diet", "protein_sort", "calorie_sort") for n in c.names)


def test_a_model_quantity_that_is_really_a_price_is_dropped():
    tokens = tokenize(prepare("snacks under 300"))
    assert constraints.drop_price_quantity(300, tokens) is None
    assert constraints.drop_price_quantity(2, tokens) == 2
    both = tokenize(prepare("2 snacks under 2"))
    assert constraints.drop_price_quantity(2, both) == 2  # the other 2 is a real quantity


def test_a_price_word_is_never_an_order_number():
    result = understand("under 300")
    assert result.entities.order_number is None
    assert understand("track order ab1234").entities.order_number == "AB1234"


def test_the_fuzzy_stage_leaves_comparator_words_alone():
    r = understand("300 ke andar snacks")
    assert r.entities.quantity is None and r.stage.value in ("rules", "fuzzy")


def test_category_slug_in_picks_the_longest_full_name():
    cats = (("snacks", "Snacks"), ("nuts-seeds-snacks", "Nuts, Seeds & Snacks"))
    assert constraints.category_slug_in(tokenize("nuts seeds and snacks"), cats) == (
        "nuts-seeds-snacks"
    )
    assert constraints.category_slug_in(tokenize("veg snacks"), cats) == "snacks"
    assert constraints.category_slug_in(tokenize("veg"), cats) is None

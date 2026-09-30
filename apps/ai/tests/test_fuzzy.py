import pytest

from apps.ai.intents import Intent, Stage
from apps.ai.pipeline import IntentPipeline

PRODUCTS = ["Makhana Crunch", "Roasted Chana Mix", "Jowar Puffs", "Plant Protein Mix"]
CATEGORIES = ["Nuts, Seeds & Snacks", "Protein Bowls"]


@pytest.mark.parametrize(
    ("text", "intent", "field", "value"),
    [
        ("makana", Intent.PRODUCT_SEARCH, "product_query", "makhana"),
        ("protien", Intent.PRODUCT_SEARCH, "product_query", "protein"),
        ("mujhe protien chahiye", Intent.PRODUCT_SEARCH, "product_query", "protein"),
        ("add 2 makana", Intent.ADD_TO_CART, "product_query", "makhana"),
        ("chekout", Intent.CHECKOUT, None, None),
        ("paymnet status", Intent.PAYMENT_STATUS, None, None),
        ("trak my order", Intent.TRACKING, None, None),
    ],
)
def test_typos_are_corrected_by_the_fuzzy_stage(text, intent, field, value):
    result = IntentPipeline().understand(text)
    assert result.intent is intent
    assert result.stage is Stage.FUZZY
    if field:
        assert getattr(result.entities, field) == value


def test_catalog_hint_improves_a_typo():
    result = IntentPipeline().understand("makhna crunch kavali", product_names=PRODUCTS)
    assert result.intent is Intent.PRODUCT_SEARCH
    assert result.stage is Stage.FUZZY
    assert result.entities.product_query == "Makhana Crunch"


def test_exact_catalog_hint_is_a_rules_match():
    result = IntentPipeline().understand("roasted chana mix kavali", product_names=PRODUCTS)
    assert result.stage is Stage.RULES
    assert result.entities.product_query == "Roasted Chana Mix"


def test_category_hint():
    result = IntentPipeline().understand("nuts seeds snacks", category_names=CATEGORIES)
    assert result.intent is Intent.CATEGORY_SELECT
    assert result.entities.category_query == "Nuts, Seeds & Snacks"


def test_whole_name_fuzzy_match_against_hints():
    result = IntentPipeline().understand("jowar pufs", product_names=PRODUCTS)
    assert result.intent is Intent.PRODUCT_SEARCH
    assert result.stage is Stage.FUZZY
    assert result.entities.product_query == "Jowar Puffs"


def test_works_without_hints():
    result = IntentPipeline().understand("makhna crunch kavali")
    assert result.intent is Intent.PRODUCT_SEARCH
    assert result.entities.product_query == "makhana crunch"


def test_non_string_hints_are_ignored():
    hints = ["Makhana Crunch", 5, None, b"bytes", "", "x" * 500]
    result = IntentPipeline().understand("makhana crunch", product_names=hints)
    assert result.entities.product_query == "Makhana Crunch"


def test_a_string_is_not_treated_as_a_list_of_hints():
    result = IntentPipeline().understand("makhana", product_names="Makhana Crunch")
    assert result.entities.product_query == "makhana"


@pytest.mark.parametrize("text", ["tomorrow evening", "something light for dinner tonight"])
def test_unrelated_words_are_not_forced_into_matches(text):
    result = IntentPipeline().understand(text, product_names=PRODUCTS)
    assert result.intent is Intent.FALLBACK

import pytest

from apps.ai.intents import Intent
from apps.ai.pipeline import IntentPipeline
from apps.ai.quantities import parse_quantity


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # digits
        ("2", (2, None)),
        ("add 12 makhana", (12, None)),
        ("5 bottles", (5, "bottle")),
        ("3pcs", (3, "piece")),
        ("2packets", (2, "packet")),
        ("2x", (2, None)),
        ("x3", (3, None)),
        ("५ पैकेट", (5, "packet")),  # Devanagari digit
        ("౩ ప్యాకెట్లు", (3, "packet")),  # Telugu digit
        # English words
        ("two packets", (2, "packet")),
        ("one packet", (1, "packet")),
        ("a dozen", (12, None)),
        # Hindi words
        ("teen packet", (3, "packet")),
        ("do packet makhana", (2, "packet")),
        ("ek dibba", (1, "box")),
        ("मखाना तीन", (3, None)),
        ("चार पीस", (4, "piece")),
        # Telugu words
        ("rendu packets", (2, "packet")),
        ("moodu", (3, None)),
        ("రెండు ప్యాకెట్లు", (2, "packet")),
        ("పది", (10, None)),
        # no maximum here: the Backend Port enforces limits
        ("100 packets", (100, "packet")),
        # not quantities
        ("do you have makhana", (None, None)),
        ("the second one", (None, None)),
        ("quinoa 500 g", (None, None)),
        ("atta 1 kg", (None, None)),
        ("9876543210", (None, None)),
        ("IVA1002", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


def test_large_quantity_is_passed_through_unchanged():
    result = IntentPipeline().understand("add 25 packets makhana")
    assert result.intent is Intent.ADD_TO_CART
    assert result.entities.quantity == 25


def test_zero_is_passed_through_for_the_port_to_reject():
    result = IntentPipeline().understand("make oats 0")
    assert result.intent is Intent.UPDATE_QUANTITY
    assert result.entities.quantity == 0


def test_ambiguous_number_word_needs_a_unit_or_product():
    assert IntentPipeline().understand("do packet oats add karo").entities.quantity == 2
    assert IntentPipeline().understand("do you have oats").entities.product_query == "oats"

"""Hindi fractional prices and the routing cues. Pure functions."""

from decimal import Decimal

import pytest

from apps.ai.constraints import parse, prepare
from apps.ai.pipeline import IntentPipeline
from apps.ai.recommend import advice_cue_matches, goal_question_matches, routing_cue_matches
from apps.ai.text import tokenize


def bounds(text):
    c = parse(tokenize(prepare(text)))
    return c.min_price, c.max_price


@pytest.mark.parametrize(
    "text, value",
    [
        ("dhai sau ke andar", 250),
        ("dedh sau ke andar", 150),
        ("saade teen sau se kam", 350),
        ("saade paanch sau tak", 550),
        ("paune do sau ke andar", 175),
        ("under dhai sau", 250),
        ("below dedh sau rupaye", 150),
        ("ढाई सौ के अंदर", 250),
        ("डेढ़ सौ तक", 150),
        ("साढ़े तीन सौ से कम", 350),
        ("साढ़े पाँच सौ तक", 550),
        ("पौने दो सौ के अंदर", 175),
    ],
)
def test_fractional_prices_beside_a_price_word(text, value):
    assert bounds(text) == (None, Decimal(value)), text
    r = IntentPipeline().understand(text)
    assert r.entities.quantity is None, text


def test_fractional_words_alone_are_not_a_price():
    assert bounds("dhai sau") == (None, None) and bounds("dedh sau protein") == (None, None)
    assert IntentPipeline().understand("add dhai sau quinoa").entities.quantity != 250


@pytest.mark.parametrize(
    "text",
    [
        "what food do i need to take if i go to the gym",
        "what should i eat",
        "which protein is best for bulking",
        "gym ke liye kya lun",
        "gym ki velte emi tinali",
        "जिम जाता हूँ, कौन सा खाना लेना चाहिए",
        "వర్కౌట్ తర్వాత ఏమి తినాలి",
        "suggest something",
    ],
)
def test_routing_cues(text):
    assert routing_cue_matches(text), text


@pytest.mark.parametrize(
    "text",
    ["quinoa", "protein bowls", "show my cart", "gym", "do you have makhana", "add 2 quinoa"],
)
def test_not_routing_cues(text):
    assert not routing_cue_matches(text), text


def test_goal_plus_question_needs_both_halves():
    assert goal_question_matches("what protein bars")
    assert not goal_question_matches("what is my order status")  # no goal word
    assert not goal_question_matches("gym")  # no question word
    assert not advice_cue_matches("what protein bars")  # a routing cue, not an advice cue

"""Table-driven tests for the whole-message "thanks" / "bye" matcher (apps.ai.closing)."""

import pytest

from apps.ai import closing
from apps.ai.closing import BYE_PHRASES, THANKS_PHRASES, classify
from apps.ai.intents import (
    CLASSIFIABLE_INTENTS,
    REPLY_INTENTS,
    Intent,
    Stage,
    parse_reply_id,
)
from apps.ai.pipeline import IntentPipeline

THANKS = Intent.THANKS
BYE = Intent.BYE


@pytest.mark.parametrize(
    "text",
    [
        "thanks", "Thanks", "THANKS!", "thank you", "Thank You.", "thank u", "thx", "ty",
        "thanks a lot", "many thanks", "ok thanks", "dhanyavaad", "dhanyavad", "shukriya",
        "धन्यवाद", "शुक्रिया", "ధన్యవాదాలు", "థాంక్స్", "dhanyavadalu",
        "thankssss", "thank youuuu", "thanks 🙏", "🙏 thanks!!", "  thanks  ", "thx!!!",
        "Thanks, thank you",
    ],
)  # fmt: skip
def test_thanks_positives(text):
    assert classify(text) is THANKS


@pytest.mark.parametrize(
    "text",
    [
        "bye", "Bye!", "bye bye", "goodbye", "see you", "see ya", "cya", "good night",
        "take care", "alvida", "अलविदा", "బై", "byeee", "bye 👋", "Good Night!!",
    ],
)  # fmt: skip
def test_bye_positives(text):
    assert classify(text) is BYE


@pytest.mark.parametrize(
    "text",
    ["thanks bye", "bye thanks", "thank you, bye!", "thanks, good night", "dhanyavaad alvida"],
)
def test_bye_wins_when_both_are_present(text):
    assert classify(text) is BYE


@pytest.mark.parametrize(
    "text",
    [
        "thanks for the menu, show snacks",
        "thanks, add 2 makhana",
        "bye the way show snacks",
        "thank you for the order, where is it",
        "show snacks thanks",
        "I want to say thanks to the team for the great service",
        "good morning",
        "hi",
        "yes",
        "no thanks i do not want it",
        "thanksgiving gift box",
        "bypass",
        "tyre",
        "Flat 12, Thanks Road, Madhapur",
        "bye bye bye bye bye bye bye bye bye",  # longer than any real closing message
        "", "   ", "🙏", "!!!", None,
    ],
)  # fmt: skip
def test_negatives_are_not_closing_messages(text):
    assert classify(text) is None


def test_every_listed_phrase_matches_its_own_intent():
    for phrase in THANKS_PHRASES:
        assert classify(phrase) is THANKS, phrase
    for phrase in BYE_PHRASES:
        assert classify(phrase) is BYE, phrase


def test_the_two_lists_do_not_share_a_phrase():
    assert closing._THANKS.isdisjoint(closing._BYE)


def test_repeated_letters_collapse_for_ascii_only():
    assert closing.fold("Thankssss") == "thanks"
    assert closing.fold("ధన్యవాదాలు") == "ధన్యవాదాలు"  # Indic text is never collapsed


def test_pipeline_returns_the_intent_from_rules_with_no_llm():
    pipeline = IntentPipeline()  # no model at all
    cases = (("thanks", THANKS), ("Thank you!", THANKS), ("bye", BYE), ("thanks bye", BYE))
    for text, intent in cases:
        result = pipeline.understand(text)
        assert (result.intent, result.stage) == (intent, Stage.RULES)
        assert result.confidence == 1.0


def test_pipeline_leaves_real_requests_alone():
    result = IntentPipeline().understand("thanks for the menu, show snacks")
    assert result.intent not in (THANKS, BYE)


def test_neither_intent_can_come_from_the_model_or_a_reply_id():
    for intent in (THANKS, BYE):
        assert intent not in CLASSIFIABLE_INTENTS
        assert intent not in REPLY_INTENTS
        assert parse_reply_id(intent.value) is None

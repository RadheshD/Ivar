"""Matcher tests for the light acknowledgement (ACK: "ok", "cool", ...)."""

import pytest

from apps.ai.closing import ACK_PHRASES, classify
from apps.ai.intents import CLASSIFIABLE_INTENTS, REPLY_INTENTS, Intent, parse_reply_id
from apps.ai.pipeline import IntentPipeline

ACK = Intent.ACK


@pytest.mark.parametrize(
    "text",
    [
        "ok", "OK", "Ok!", "okay", "k", "kk", "okkk", "cool", "Cool.", "coool", "great", "nice",
        "fine", "alright", "all right", "got it", "Got it!", "noted", "ok ok", "ok cool",
        "theek hai", "thik hai", "accha", "sare", "సరే", "ठीक है", "👍", "👍🏽", "👍 👍", "ok 👍",
    ],
)  # fmt: skip
def test_ack_positives(text):
    assert classify(text) is ACK


@pytest.mark.parametrize(
    "text",
    [
        "yes", "yeah", "yep", "sure", "haan", "ha", "no", "nope", "nahi", "avunu",
        "ok show snacks", "cool, add 2 makhana", "fine, checkout", "got it thanks for the menu",
        "great job team", "nice to meet you", "👎", "👍 show snacks", "🙏",
    ],
)  # fmt: skip
def test_ack_negatives(text):
    assert classify(text) is not ACK


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("ok thanks", Intent.THANKS), ("cool thanks", Intent.THANKS),
        ("thanks 👍", Intent.THANKS), ("ok thank you", Intent.THANKS),
        ("great, thank you", Intent.THANKS), ("theek hai dhanyavaad", Intent.THANKS),
        ("ok bye", Intent.BYE), ("cool bye", Intent.BYE), ("ok thanks bye", Intent.BYE),
        ("👍 bye", Intent.BYE), ("sare bye", Intent.BYE),
    ],
)  # fmt: skip
def test_thanks_and_bye_win_over_ack(text, intent):
    assert classify(text) is intent


def test_ack_phrases_do_not_include_real_answers():
    for word in ("yes", "yeah", "sure", "haan", "no"):
        assert word not in ACK_PHRASES
    for phrase in ACK_PHRASES:
        assert classify(phrase) is ACK, phrase


def test_the_pipeline_never_returns_ack():
    pipeline = IntentPipeline()
    for text in ("ok", "cool", "👍", "theek hai"):
        assert pipeline.understand(text).intent is not ACK


def test_ack_cannot_come_from_the_model_or_a_reply_id():
    assert ACK not in CLASSIFIABLE_INTENTS
    assert ACK not in REPLY_INTENTS
    assert parse_reply_id("ACK") is None

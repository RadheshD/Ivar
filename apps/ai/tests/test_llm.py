import inspect
import logging
import time

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.ai import pipeline as pipeline_module
from apps.ai.intents import CLASSIFIABLE_INTENTS, TEXT_ENTITY_FIELDS, Intent, LLMOutcome, Stage
from apps.ai.llm import (
    FakeLLMClient,
    LLMClient,
    LLMError,
    LLMTimeout,
    build_prompt,
    client_from_settings,
    timeout_from_settings,
)
from apps.ai.pipeline import IntentPipeline, pipeline_from_settings

# Nothing in the keyword tables: rules and fuzzy cannot decide, so the LLM is asked.
UNDECIDED = "can we talk about the weather"
PHONE = "919876541234"

reply = FakeLLMClient.reply


def test_fake_client_implements_the_interface():
    assert isinstance(FakeLLMClient(), LLMClient)


def test_llm_success():
    llm = FakeLLMClient([reply("PRODUCT_SEARCH", 0.85, product_query="light snacks")])
    result = IntentPipeline(llm).understand(UNDECIDED)
    assert result.intent is Intent.PRODUCT_SEARCH
    assert result.stage is Stage.LLM
    assert result.llm_outcome is LLMOutcome.OK
    assert result.confidence == 0.85
    assert result.entities.product_query == "light snacks"
    assert llm.calls == 1


def test_llm_not_called_when_rules_are_confident():
    llm = FakeLLMClient([reply("HELP")])
    result = IntentPipeline(llm).understand("show my cart")
    assert result.intent is Intent.VIEW_CART
    assert result.llm_outcome is LLMOutcome.NOT_CALLED
    assert llm.calls == 0


def test_llm_irrelevant_entities_are_dropped():
    llm = FakeLLMClient([reply("GREETING", 0.9, product_query="makhana", quantity=3)])
    result = IntentPipeline(llm).understand(UNDECIDED)
    assert result.intent is Intent.GREETING
    assert result.entities.product_query is None
    assert result.entities.quantity is None


def test_low_confidence_becomes_clarify():
    llm = FakeLLMClient([reply("ADD_TO_CART", 0.4, product_query="oats", quantity=2)])
    result = IntentPipeline(llm).understand(UNDECIDED)
    assert result.intent is Intent.CLARIFY
    assert result.suggested_intent is Intent.ADD_TO_CART
    assert result.stage is Stage.LLM
    assert result.llm_outcome is LLMOutcome.LOW_CONFIDENCE


def test_threshold_is_configurable():
    llm = FakeLLMClient([reply("HELP", 0.75)])
    result = IntentPipeline(llm, confidence_threshold=0.8).understand(UNDECIDED)
    assert result.intent is Intent.CLARIFY


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (LLMTimeout("slow"), LLMOutcome.TIMEOUT),
        (LLMError("down"), LLMOutcome.ERROR),
        (RuntimeError("boom"), LLMOutcome.ERROR),
        ("{intent: GREETING", LLMOutcome.INVALID_JSON),
        ('```json\n{"intent": "HELP"}\n```', LLMOutcome.INVALID_JSON),
        ("", LLMOutcome.INVALID_JSON),
        (reply("BUY_STOCK"), LLMOutcome.INVALID_SCHEMA),
        (reply("CLARIFY"), LLMOutcome.INVALID_SCHEMA),
        ('{"intent": "HELP", "confidence": 0.9}', LLMOutcome.INVALID_SCHEMA),
    ],
)
def test_llm_failure_falls_back(response, outcome):
    result = IntentPipeline(FakeLLMClient([response])).understand(UNDECIDED)
    assert result.intent is Intent.FALLBACK
    assert result.stage is Stage.NONE
    assert result.llm_outcome is outcome


def test_llm_non_text_reply_is_invalid_json():
    class NumberClient:
        def complete(self, prompt, *, timeout):
            return 123

    result = IntentPipeline(NumberClient()).understand(UNDECIDED)
    assert result.intent is Intent.FALLBACK
    assert result.llm_outcome is LLMOutcome.INVALID_JSON


def test_slow_llm_is_abandoned_at_the_timeout():
    llm = FakeLLMClient([reply("HELP")], delay=1.0)
    started = time.monotonic()
    result = IntentPipeline(llm, llm_timeout=0.05).understand(UNDECIDED)
    assert time.monotonic() - started < 0.5
    assert result.intent is Intent.FALLBACK
    assert result.llm_outcome is LLMOutcome.TIMEOUT


def test_llm_failure_keeps_a_low_confidence_rules_candidate_as_clarify():
    llm = FakeLLMClient([LLMTimeout("slow")])
    result = IntentPipeline(llm).understand("naaku kavali")  # "I want" + nothing
    assert result.intent is Intent.CLARIFY
    assert result.suggested_intent is Intent.PRODUCT_SEARCH
    assert result.stage is Stage.RULES
    assert result.llm_outcome is LLMOutcome.TIMEOUT


def test_no_llm_configured():
    result = IntentPipeline().understand(UNDECIDED)
    assert result.intent is Intent.FALLBACK
    assert result.llm_outcome is LLMOutcome.NOT_CONFIGURED


def test_phone_numbers_are_masked_before_reaching_the_llm():
    llm = FakeLLMClient([reply("HELP")])
    IntentPipeline(llm).understand(f"call me on {PHONE} about dinner")
    assert PHONE not in llm.prompts[0]
    assert "91******1234" in llm.prompts[0]


def test_prompt_builder_accepts_no_customer_data():
    # The only inputs are the message, a language guess and public catalog names; there is
    # no parameter through which a name, city, address, cart or order could be passed.
    params = set(inspect.signature(build_prompt).parameters)
    assert params == {"message", "language_hint", "product_names", "category_names"}


def test_prompt_contains_only_the_masked_message_and_the_schema():
    llm = FakeLLMClient([reply("HELP")])
    IntentPipeline(llm).understand(f"weather talk {PHONE}")
    prompt = llm.prompts[0]
    lines = prompt.splitlines()
    assert lines[-1] == 'Customer message (JSON string): "weather talk 91******1234"'
    assert PHONE not in prompt
    for intent in CLASSIFIABLE_INTENTS:
        assert intent.value in prompt
    assert all(field in prompt for field in TEXT_ENTITY_FIELDS)
    # Every other line is fixed instruction text, the (empty) hints or the language guess.
    expected_prefixes = (
        "You classify", "Reply with", "{", "Allowed intents", "Allowed units",
        "product_query and", "Known product names: []", "Known category names: []",
        "Message language guess: ", "Customer message",
    )  # fmt: skip
    assert all(line.startswith(expected_prefixes) for line in lines)


def test_catalog_hints_reach_the_prompt():
    llm = FakeLLMClient([reply("HELP")])
    IntentPipeline(llm).understand(UNDECIDED, product_names=["Makhana Crunch"])
    assert "Makhana Crunch" in llm.prompts[0]


@pytest.mark.parametrize("response", [LLMError("down"), "not json", reply("NOPE")])
def test_failure_logs_carry_no_customer_text(caplog, response):
    text = f"dinner ideas {PHONE}"
    with caplog.at_level(logging.DEBUG, logger="apps.ai"):
        IntentPipeline(FakeLLMClient([response])).understand(text)
    messages = [r.getMessage() for r in caplog.records]
    assert any("LLM fallback failed" in m for m in messages)
    for message in messages:
        assert PHONE not in message
        assert "dinner" not in message


def test_pipeline_never_raises(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError(f"internal detail {PHONE}")

    monkeypatch.setattr(pipeline_module.rules, "match", broken)
    result = IntentPipeline().understand("show my cart")
    assert result.intent is Intent.FALLBACK
    assert all(PHONE not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", [None, "", "   ", "!!!", "🙂🙂", 42, ["list"]])
def test_empty_or_odd_input_is_fallback(text):
    result = IntentPipeline(FakeLLMClient()).understand(text)
    assert result.intent is Intent.FALLBACK
    assert result.stage is Stage.NONE


def test_invalid_pipeline_settings():
    with pytest.raises(ValueError):
        IntentPipeline(confidence_threshold=0)
    with pytest.raises(ValueError):
        IntentPipeline(llm_timeout=0)


@override_settings(LLM_PROVIDER="")
def test_no_provider_means_no_client():
    assert client_from_settings() is None
    assert isinstance(pipeline_from_settings(), IntentPipeline)


@override_settings(LLM_PROVIDER="someprovider")
def test_unimplemented_provider_is_a_configuration_error():
    with pytest.raises(ImproperlyConfigured):
        client_from_settings()


@pytest.mark.parametrize(("value", "expected"), [("2.5", 2.5), (3, 3.0)])
def test_timeout_from_settings(value, expected):
    with override_settings(LLM_TIMEOUT_SECONDS=value):
        assert timeout_from_settings() == expected


@pytest.mark.parametrize("value", ["abc", "0", -1, None])
def test_bad_timeout_setting(value):
    with override_settings(LLM_TIMEOUT_SECONDS=value), pytest.raises(ImproperlyConfigured):
        timeout_from_settings()


def test_fake_reply_rejects_unknown_fields():
    with pytest.raises(ValueError):
        FakeLLMClient.reply("HELP", reference_id="x")

"""The LLM fallback inside the engine: when it runs, what it may do, and how it fails.

A scripted fake classifier stands in for Groq; no network.
"""

from __future__ import annotations

import json
import uuid
from collections import deque

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai import llm_guard
from apps.ai.classifier import (
    ClassifierHttpError,
    ClassifierRateLimited,
    ClassifierReply,
    ClassifierTimeout,
)
from apps.ai.intents import LLMOutcome
from apps.ai.llm_fallback import LLMFallback
from apps.ai.llm_guard import CircuitBreaker, LLMGuard
from apps.ai.pipeline import IntentPipeline
from apps.conversations.channel import Inbound
from apps.conversations.engine import ConversationEngine

from .harness import ADDRESS, WA_ID, Chat, onboard

NOISE = "zork florp"  # nothing in the rules understands this


def reply(intent="ADD_TO_CART", confidence=0.92, items=None, ambiguous=False, language="en"):
    return json.dumps(
        {
            "intent": intent,
            "confidence": confidence,
            "items": items if items is not None else [],
            "language": language,
            "ambiguous": ambiguous,
        }
    )


def item(slug, quantity=None, mention="x"):
    return {"product_slug": slug, "mention": mention, "quantity": quantity}


class FakeClassifier:
    def __init__(self, *responses):
        self.queue = deque(responses)
        self.messages: list[str] = []
        self.catalogs = []

    @property
    def calls(self):
        return len(self.messages)

    def classify(self, message, *, catalog, timeout):
        self.messages.append(message)
        self.catalogs.append(catalog)
        item = self.queue.popleft()
        if isinstance(item, Exception):
            raise item
        return ClassifierReply(item, "fake-model", 100, 20)


@pytest.fixture(autouse=True)
def local_cache(monkeypatch):
    monkeypatch.setattr(llm_guard, "cache", LocMemCache(f"engine-llm-{uuid.uuid4().hex}", {}))


@pytest.fixture
def fake():
    return FakeClassifier()


@pytest.fixture
def guard():
    return LLMGuard(daily_cap=100, per_customer_per_minute=0, breaker=CircuitBreaker(3, 60))


@pytest.fixture
def llm_chat(backend, store, clock, fake, guard):
    pipeline = IntentPipeline(fallback=LLMFallback(fake, guard, min_confidence=0.7, timeout=2))
    return Chat(ConversationEngine(backend, store, pipeline, clock=clock))


@pytest.fixture
def plain_chat(backend, store, clock):
    """Same engine with the LLM feature off (the pipeline has no fallback)."""
    return Chat(ConversationEngine(backend, store, IntentPipeline(), clock=clock))


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(WA_ID)
    return {ln.product_id: ln.quantity for ln in backend.get_cart(customer.id).lines}


def send_llm(llm_chat, fake, text, *responses):
    fake.queue.extend(responses)
    return llm_chat.send(text)


# ---------------------------------------------------------------- cart changes need a Yes


def test_add_asks_for_confirmation_and_changes_nothing_until_yes(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("classic-oats-bowl", 2, "oats bowl")]))
    assert llm_chat.keys == ["llm.confirm_add"]
    assert llm_chat.message("llm.confirm_add").body == "Add 2 × Classic Oats Bowl?"
    assert llm_chat.ids == ["ACT:llmyes", "ACT:llmno"]
    assert cart_of(llm_chat) == {}
    llm_chat.tap("ACT:llmyes")
    assert llm_chat.keys[0] == "llm.done" and "cart.title" in llm_chat.keys
    assert cart_of(llm_chat) == {"classic-oats-bowl": 2}


def test_no_cancels_without_touching_the_cart(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("quinoa", 1)]))
    llm_chat.tap("ACT:llmno")
    assert llm_chat.keys == ["llm.cancelled"]
    assert cart_of(llm_chat) == {}
    llm_chat.tap("ACT:llmyes")  # a stale Yes after the No does nothing
    assert cart_of(llm_chat) == {}
    assert llm_chat.keys == ["menu.body"]


def test_typing_something_else_instead_of_answering_cancels_the_request(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("quinoa", 1)]))
    llm_chat.send("show my cart")
    llm_chat.tap("ACT:llmyes")
    assert cart_of(llm_chat) == {}


def test_missing_quantity_defaults_to_one_in_the_question(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("quinoa", None)]))
    assert "1 × Quinoa" in llm_chat.body


def test_multiple_items_are_confirmed_together(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("quinoa", 1), item("chia-seeds", 2)]))
    llm_chat.tap("ACT:llmyes")
    assert cart_of(llm_chat) == {"quinoa": 1, "chia-seeds": 2}


def test_out_of_stock_item_is_not_offered(llm_chat, fake, backend):
    onboard(llm_chat)
    backend.set_stock("quinoa", False)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("quinoa", 1)]))
    assert "llm.confirm_add" not in llm_chat.keys
    assert cart_of(llm_chat) == {}


def test_hallucinated_slug_never_reaches_the_cart(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(items=[item("unicorn-tea", 3, "unicorn tea")]))
    assert "llm.confirm_add" not in llm_chat.keys
    assert cart_of(llm_chat) == {}


def test_remove_and_update_also_need_confirmation(llm_chat, fake):
    onboard(llm_chat)
    llm_chat.send("add 2 makhana crunch")
    send_llm(llm_chat, fake, NOISE, reply("UPDATE_QUANTITY", items=[item("makhana-crunch", 5)]))
    assert llm_chat.keys == ["llm.confirm_update"]
    assert cart_of(llm_chat) == {"makhana-crunch": 2}
    llm_chat.tap("ACT:llmyes")
    assert cart_of(llm_chat) == {"makhana-crunch": 5}
    send_llm(llm_chat, fake, NOISE, reply("REMOVE_FROM_CART", items=[item("makhana-crunch")]))
    assert llm_chat.keys == ["llm.confirm_remove"]
    assert cart_of(llm_chat) == {"makhana-crunch": 5}
    llm_chat.tap("ACT:llmyes")
    assert cart_of(llm_chat) == {}


# ---------------------------------------------------------------- read-only: no confirmation


def test_search_needs_no_confirmation(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply("PRODUCT_SEARCH", items=[item("quinoa", None, "quinoa")]))
    assert not any(k and k.startswith("llm.") for k in llm_chat.keys)
    assert any("Quinoa" in r.title for r in llm_chat.rows)
    assert cart_of(llm_chat) == {}


@pytest.mark.parametrize(
    ("intent", "key"),
    [("VIEW_CART", "cart.empty"), ("HELP", "help"), ("ORDER_STATUS", "order.none")],
)
def test_other_read_only_intents_use_the_normal_handlers(llm_chat, fake, intent, key):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(intent))
    assert not any(k and k.startswith("llm.") for k in llm_chat.keys)
    assert llm_chat.keys[0] == key or key in llm_chat.keys


def test_ambiguous_product_shows_matches_instead_of_acting(llm_chat, fake):
    onboard(llm_chat)
    send_llm(
        llm_chat, fake, NOISE,
        reply(items=[item(None, 1, "oats")], ambiguous=True),
    )  # fmt: skip
    assert "llm.confirm_add" not in llm_chat.keys
    assert cart_of(llm_chat) == {}


def test_low_confidence_asks_a_clarifying_question(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply(confidence=0.3, items=[item("quinoa", 1)]))
    assert llm_chat.keys == ["clarify.ADD_TO_CART"]
    assert cart_of(llm_chat) == {}


def test_unknown_intent_uses_the_existing_no_match_reply(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, reply("unknown", 0.9))
    assert llm_chat.keys == ["fallback"]


# ---------------------------------------------------------------- failures are silent


@pytest.mark.parametrize(
    "failure",
    [
        ClassifierTimeout(),
        ClassifierRateLimited(),
        ClassifierHttpError("http_error"),
        "not json at all",
        json.dumps({"intent": "ADD_TO_CART"}),
        RuntimeError("anything"),
    ],
    ids=["timeout", "429", "http", "bad-json", "schema", "crash"],
)
def test_failures_fall_back_to_the_normal_reply(llm_chat, fake, failure):
    onboard(llm_chat)
    send_llm(llm_chat, fake, NOISE, failure)
    assert llm_chat.keys == ["fallback"]
    assert fake.calls == 1


def test_circuit_breaker_stops_calls_after_repeated_failures(llm_chat, fake, guard):
    onboard(llm_chat)
    for _ in range(3):
        send_llm(llm_chat, fake, NOISE, ClassifierTimeout())
    assert fake.calls == 3
    llm_chat.send(NOISE)  # breaker is open: no call, same reply
    assert fake.calls == 3 and llm_chat.keys == ["fallback"]
    assert guard.breaker.is_open


def test_daily_cap_stops_calls(llm_chat, fake, backend, store, clock):
    guard = LLMGuard(daily_cap=2, per_customer_per_minute=0)
    pipeline = IntentPipeline(fallback=LLMFallback(fake, guard, min_confidence=0.7))
    chat = Chat(ConversationEngine(backend, store, pipeline, clock=clock))
    onboard(chat)
    for _ in range(4):
        fake.queue.append(reply("unknown"))
        chat.send(NOISE)
    assert fake.calls == 2


def test_per_customer_limit_stops_calls(backend, store, clock, fake):
    guard = LLMGuard(daily_cap=100, per_customer_per_minute=1)
    pipeline = IntentPipeline(fallback=LLMFallback(fake, guard, min_confidence=0.7))
    chat = Chat(ConversationEngine(backend, store, pipeline, clock=clock))
    onboard(chat)
    for _ in range(3):
        fake.queue.append(reply("unknown"))
        chat.send(NOISE)
    assert fake.calls == 1


# ---------------------------------------------------------------- when it must NOT run


def test_disabled_flag_means_zero_calls(plain_chat, fake):
    onboard(plain_chat)
    plain_chat.send(NOISE)
    assert plain_chat.keys == ["fallback"] and fake.calls == 0


def test_rules_that_understand_never_call_the_llm(llm_chat, fake):
    onboard(llm_chat)
    llm_chat.send("show my cart")
    assert fake.calls == 0


def test_skipped_during_onboarding_and_language_selection(llm_chat, fake):
    llm_chat.send(NOISE)  # brand-new customer: language prompt
    assert fake.calls == 0
    llm_chat.send(NOISE)
    assert fake.calls == 0


def test_skipped_for_button_and_list_replies(llm_chat, fake):
    onboard(llm_chat)
    llm_chat.tap("ACT:more", "Show more")
    llm_chat.tap("totally-unknown-id", "zork florp")
    assert fake.calls == 0


def test_skipped_during_address_entry_payment_and_confirmation(llm_chat, fake):
    onboard(llm_chat)
    llm_chat.send("add 2 makhana crunch")
    llm_chat.send("checkout")
    llm_chat.send("Hi")  # exact-match greeting at the address step is still the rules' job
    assert fake.calls == 0
    llm_chat.send(ADDRESS)
    assert "checkout.confirm" in llm_chat.keys
    llm_chat.send(NOISE)  # order-confirmation step
    assert fake.calls == 0
    llm_chat.tap("ACT:confirm")
    assert "checkout.created" in llm_chat.keys
    llm_chat.send(NOISE)  # awaiting payment
    assert fake.calls == 0


def test_skipped_for_catalog_orders_and_plan_messages(llm_chat, fake):
    onboard(llm_chat)
    order = {"catalog_id": "c", "text": None, "unreadable": 0, "items": [
        {"product_id": "quinoa", "quantity": 1, "price": None, "currency": None}
    ]}  # fmt: skip
    llm_chat.last = llm_chat.engine.handle(WA_ID, Inbound(order=order))
    llm_chat.send("IVA PLAN v1\ngoal: healthy_eating\npicks: Makhana Crunch")
    assert fake.calls == 0


def test_skipped_while_a_question_is_pending(llm_chat, fake):
    onboard(llm_chat)
    llm_chat.send("add makhana")  # asks how many: a bare answer is expected next
    assert llm_chat.session().pending is not None
    llm_chat.send(NOISE)
    assert fake.calls == 0


# ---------------------------------------------------------------- what is sent / logged


def test_only_redacted_text_and_the_catalog_allow_list_are_sent(llm_chat, fake):
    onboard(llm_chat)
    send_llm(llm_chat, fake, "zork florp 9876543210 a@b.co", reply("unknown"))
    sent = fake.messages[0]
    assert "9876543210" not in sent and "a@b.co" not in sent
    catalog = fake.catalogs[0]
    assert len(catalog) == 55 and catalog[0].slug and catalog[0].name
    assert all(set(vars(e)) == {"slug", "name", "category"} for e in catalog)
    assert WA_ID not in sent


def test_logs_have_no_customer_text(llm_chat, fake, caplog):
    import logging

    onboard(llm_chat)
    with caplog.at_level(logging.DEBUG):
        send_llm(llm_chat, fake, "zork florp private words", reply(items=[item("quinoa", 1)]))
    assert "private words" not in caplog.text and "zork" not in caplog.text
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("llm: outcome"))
    assert "outcome=ok" in line and "intent=ADD_TO_CART" in line and "quinoa" in line
    assert "latency_ms=" in line and "model=fake-model" in line and "prompt_tokens=100" in line


def test_outcome_enum_covers_the_new_categories():
    assert {o.value for o in (LLMOutcome.SKIPPED, LLMOutcome.UNKNOWN, LLMOutcome.AMBIGUOUS)} == {
        "skipped",
        "unknown",
        "ambiguous",
    }


# ---------------------------------------------------------------- golden set + eval command


def test_golden_set_is_well_formed_and_covers_every_style():
    from apps.conversations.management.commands.eval_intent_llm import load_golden

    entries = load_golden()
    assert 75 <= len(entries) <= 100
    assert len({e["id"] for e in entries}) == len(entries)
    assert {e["language"] for e in entries} == {"en", "hi", "te", "hinglish", "tenglish", "mixed"}
    tags = {t for e in entries for t in e["tags"]}
    assert {"typo", "injection", "off_topic", "phone_redaction", "email_redaction",
            "ambiguous_product", "quantity_words", "multi_item"} <= tags  # fmt: skip
    assert all(e["needs_native_review"] for e in entries if e["language"] != "en")
    from apps.ai.intents import CLASSIFIABLE_INTENTS

    valid = {i.value for i in CLASSIFIABLE_INTENTS} | {"unknown"}
    assert all(e["intent"] in valid for e in entries)


def test_golden_slugs_exist_in_the_catalog(backend):
    from apps.conversations.management.commands.eval_intent_llm import load_golden

    ids = {p.id for p in backend._products.values()}
    for entry in load_golden():
        for it in entry["items"]:
            assert it["slug"] in ids, entry["id"]


def test_eval_command_refuses_outside_dev_mode(settings):
    settings.IVA_DEV_RESET_ALLOWED = False
    settings.GROQ_API_KEY = "gsk_never_used"
    with pytest.raises(CommandError, match="development"):
        call_command("eval_intent_llm")


def test_eval_command_needs_a_key(settings):
    settings.IVA_DEV_RESET_ALLOWED = True
    settings.GROQ_API_KEY = ""
    with pytest.raises(CommandError, match="GROQ_API_KEY"):
        call_command("eval_intent_llm")

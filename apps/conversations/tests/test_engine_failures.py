"""Transcripts: backend down, LLM down, expired session, CLARIFY, bugs, store failures."""

import logging

import pytest

from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline
from apps.conversations.backend_port import (
    InvalidQuantity,
    NotAuthorized,
    NotFound,
    ProductUnavailable,
)
from apps.conversations.channel import Inbound
from apps.conversations.state import SessionStoreError

from .harness import (
    ADDRESS,
    LANGUAGES,
    OUTAGE,
    PHRASES,
    WA_ID,
    Chat,
    checkout_to_payment,
    in_script,
    onboard,
)

reply = FakeLLMClient.reply
UNDECIDED = "can we talk about the weather"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_backend_down_keeps_the_session(chat, flaky, lang):
    onboard(chat, lang=lang)
    chat.tap("CATEGORY_SELECT:packaged")
    before = chat.session()
    flaky.fail["list_products"] = OUTAGE
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    assert chat.keys == ["error.unavailable"]
    assert in_script(chat.body, lang)
    assert chat.session() == before  # nothing half-saved

    del flaky.fail["list_products"]
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    assert chat.keys[0] == "category.body"


def test_backend_down_before_the_customer_is_known(chat, flaky):
    flaky.fail["get_or_create_customer"] = OUTAGE
    chat.send("hi")
    assert chat.keys == ["error.unavailable"]


def test_backend_down_on_add_keeps_the_pending_product(chat, flaky, backend):
    onboard(chat)
    chat.send("add makhana")
    flaky.fail["add_item"] = OUTAGE
    chat.send("2")
    assert chat.keys == ["error.unavailable"]
    del flaky.fail["add_item"]
    chat.send("2")
    assert chat.keys[0] == "cart.added"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (NotFound("x"), "error.not_found"),
        (NotAuthorized("x"), "error.not_found"),
        (ProductUnavailable("x"), "error.out_of_stock"),
        (InvalidQuantity("x"), "qty.invalid"),
    ],
)
def test_backend_errors_become_friendly_messages(chat, flaky, error, key):
    onboard(chat)
    flaky.fail["get_cart"] = error
    chat.send("show my cart")
    assert chat.keys == [key]


def test_value_error_from_the_port_is_logged_as_a_bug(chat, flaky, caplog):
    onboard(chat)
    flaky.fail["get_cart"] = ValueError(f"bad argument for {WA_ID}")
    with caplog.at_level(logging.ERROR, logger="apps.conversations"):
        chat.send("show my cart")
    assert chat.keys == ["error.generic"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("bug" in m and "ValueError" in m for m in messages)
    assert all(WA_ID not in m for m in messages)


def test_unexpected_exception_never_escapes(chat, flaky):
    onboard(chat)
    flaky.fail["get_cart"] = RuntimeError("boom")
    chat.send("show my cart")
    assert chat.keys == ["error.generic"]


def test_invalid_inbound_is_handled(engine):
    assert [m.key for m in engine.handle(WA_ID, "not an inbound")] == ["error.generic"]
    assert [m.key for m in engine.handle("", Inbound.typed("hi"))] == ["error.generic"]


def test_llm_down_still_understands_rules(chat, llm):
    onboard(chat)
    chat.send(UNDECIDED)
    assert chat.keys == ["fallback"]
    assert llm.calls >= 1  # it was tried and failed
    chat.send("show my cart")
    assert chat.keys == ["cart.empty"]


def test_llm_answer_is_used_when_rules_cannot_decide(make_engine, flaky):
    llm = FakeLLMClient(default=reply("VIEW_CART", 0.9))
    chat = Chat(make_engine(flaky, pipeline=IntentPipeline(llm)))
    onboard(chat)
    chat.send(UNDECIDED)
    assert chat.keys == ["cart.empty"]


def test_clarify_asks_and_never_acts(make_engine, flaky, backend):
    llm = FakeLLMClient(default=reply("ADD_TO_CART", 0.3, product_query="makhana", quantity=2))
    chat = Chat(make_engine(flaky, pipeline=IntentPipeline(llm)))
    onboard(chat)
    chat.send(UNDECIDED)
    assert chat.keys == ["clarify.ADD_TO_CART"]
    customer = backend.get_or_create_customer(WA_ID)
    assert backend.get_cart(customer.id).lines == ()


def test_clarify_from_rules_offers_a_confirm_button(make_engine, flaky):
    llm = FakeLLMClient(default=reply("VIEW_CART", 0.2))
    chat = Chat(make_engine(flaky, pipeline=IntentPipeline(llm)))
    onboard(chat)
    chat.send(UNDECIDED)
    assert chat.keys == ["clarify.VIEW_CART"]
    assert chat.ids == ["VIEW_CART", "CATEGORY_SELECT"]
    chat.tap("VIEW_CART")  # the customer confirms; only now does anything happen
    assert chat.keys == ["cart.empty"]


def test_clarify_without_llm_for_a_vague_request(chat):
    onboard(chat)
    chat.send("naaku kavali")
    assert chat.keys == ["clarify.PRODUCT_SEARCH"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_expired_session_resumes_at_the_main_menu(chat, clock, backend, lang):
    onboard(chat, lang=lang)
    chat.send(PHRASES[lang]["search"])  # now waiting for a quantity
    clock.advance(3601)
    chat.send("2")
    assert chat.keys == ["welcome_back", "menu.body"]
    customer = backend.get_or_create_customer(WA_ID)
    assert backend.get_cart(customer.id).lines == ()  # the old pending add was not applied


def test_expired_session_still_answers_a_clear_request(chat, clock):
    onboard(chat)
    clock.advance(3601)
    chat.send("show my cart")
    assert chat.keys == ["cart.empty"]


def test_session_store_down_falls_back_to_the_main_menu(chat, store, monkeypatch):
    onboard(chat)

    def broken(*args, **kwargs):
        raise SessionStoreError("down")

    monkeypatch.setattr(store, "load", broken)
    monkeypatch.setattr(store, "save", broken)
    chat.send("hi")
    assert chat.keys == ["welcome_back", "menu.body"]


def test_llm_prompt_never_contains_customer_data(make_engine, flaky):
    llm = FakeLLMClient(default=reply("HELP", 0.9))
    chat = Chat(make_engine(flaky, pipeline=IntentPipeline(llm)))
    onboard(chat, name="Ravi Kumar", city="Secunderabad")
    order_number = checkout_to_payment(chat)
    chat.send("add 1 makhana")  # something in the cart again
    chat.send(UNDECIDED)
    assert llm.calls >= 1
    prompt = llm.prompts[-1]
    for private in ("Ravi Kumar", "Secunderabad", ADDRESS, "Green Park", order_number, WA_ID):
        assert private not in prompt
    assert prompt.splitlines()[-1] == f'Customer message (JSON string): "{UNDECIDED}"'

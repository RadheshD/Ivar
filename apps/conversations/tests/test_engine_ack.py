"""Transcripts: BYE at the address step, and the light acknowledgement (ACK)."""

import pytest

from apps.conversations.state import State

from .harness import ADDRESS, WA_ID, checkout_to_payment, onboard

ACK_EN = "👍 Anything else I can help with?"
BYE_EN = "Bye, Ravi! 👋 Message me anytime you'd like to order."


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(WA_ID)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


def addresses_of(chat):
    backend = chat.engine.backend
    return backend.list_addresses(backend.get_or_create_customer(WA_ID).id)


def snapshot(chat):
    s = chat.session()
    return (s.state, s.pending, s.address_id, s.order_number, s.idempotency_key, cart_of(chat))


def button_titles(chat):
    return [b.title for b in chat.last[0].buttons]


# ------------------------------------------------------------------ BYE at the address step


def test_bye_at_the_address_step_is_only_a_goodbye_and_checkout_can_resume(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    before = snapshot(chat)
    chat.send("Bye")
    assert chat.keys == ["closing.bye"]  # no "Where should we deliver?" afterwards
    assert chat.body == f"{BYE_EN}\nYour cart is still saved."
    assert snapshot(chat) == before
    assert chat.session().state.name == "CHECKOUT_ADDRESS"
    assert addresses_of(chat) == []
    chat.send("Hi")
    assert chat.keys == ["checkout.welcome_back"]
    assert chat.ids == ["ACT:resume", "CATEGORY_SELECT"]
    assert chat.session().state.name == "CHECKOUT_ADDRESS"


def test_thanks_at_the_address_step_still_re_prompts(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    chat.send("thanks")
    assert chat.keys == ["closing.thanks", "checkout.type_address"]


def test_bye_at_the_confirmation_step_is_only_a_goodbye(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    chat.send(ADDRESS)
    before = snapshot(chat)
    chat.send("bye")
    assert chat.keys == ["closing.bye"]
    assert snapshot(chat) == before
    assert chat.session().state.name == "CHECKOUT_CONFIRM"


# ------------------------------------------------------------------ ACK where it applies


@pytest.mark.parametrize("text", ["ok", "Cool", "👍", "okay", "got it", "theek hai", "great"])
def test_ack_when_idle(chat, text):
    onboard(chat)
    chat.send(text)
    assert chat.keys == ["closing.ack"]
    assert chat.body == ACK_EN
    assert button_titles(chat) == ["Menu"]


@pytest.mark.parametrize("state_name", ["MAIN_MENU", "GROUP", "CATEGORY", "PRODUCT", "CART"])
def test_ack_in_every_idle_or_browsing_state(chat, state_name):
    onboard(chat)
    session = chat.session()
    session.state = State[state_name]
    chat.engine.store.save(WA_ID, session)
    chat.send("ok")
    assert chat.keys == ["closing.ack"]
    assert chat.session().state.name == state_name


def test_ack_with_items_in_the_cart_offers_view_cart_without_a_saved_claim(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    before = snapshot(chat)
    chat.send("cool")
    assert chat.keys == ["closing.ack"]
    assert chat.body == ACK_EN  # no "cart is still saved" claim
    assert button_titles(chat) == ["Menu", "View cart"]
    assert snapshot(chat) == before


def test_ack_after_an_order(chat):
    onboard(chat)
    number = checkout_to_payment(chat)
    before = snapshot(chat)
    chat.send("cool")  # was "Sorry, I didn't understand that."
    assert chat.keys == ["closing.ack"]
    assert snapshot(chat) == before
    assert chat.session().order_number == number
    assert chat.session().state.name == "AWAITING_PAYMENT"


def test_ack_with_no_session(chat, store):
    onboard(chat)
    store._data.clear()  # the session expired
    chat.send("ok")
    assert chat.keys == ["closing.ack"]


@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        ("hi", "ठीक है", "👍 क्या मैं और कुछ मदद कर सकता हूँ?"),
        ("hi", "theek hai", "👍 Aur kuch help chahiye?"),
        ("te", "సరే", "👍 ఇంకేమైనా సహాయం కావాలా?"),
        ("te", "sare", "👍 Inkemaina help kavala?"),
    ],
)
def test_ack_languages(chat, lang, text, expected):
    onboard(chat, lang)
    chat.send(text)
    assert chat.keys == ["closing.ack"]
    assert chat.body == expected


# ------------------------------------------------------------------ ACK ignored


def test_ack_is_ignored_at_the_address_step(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    before = snapshot(chat)
    chat.send("ok")  # existing handling: not an address, asked again
    assert "closing.ack" not in chat.keys
    assert chat.keys == ["checkout.address_invalid"]
    assert addresses_of(chat) == []
    assert snapshot(chat) == before


def test_ack_is_ignored_at_the_confirmation_step(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    chat.send(ADDRESS)
    assert "checkout.confirm" in chat.keys
    chat.send("ok")
    assert "closing.ack" not in chat.keys
    assert chat.session().state.name == "CHECKOUT_CONFIRM"
    assert chat.session().order_number is None  # an "ok" did not place the order


def test_ack_is_ignored_while_a_quantity_question_is_pending(chat):
    onboard(chat)
    chat.send("add makhana")
    before = chat.session()
    assert before.state.name == "QUANTITY" and before.pending["kind"] == "add"
    chat.send("ok")  # existing handling: asks for the quantity again
    assert chat.keys == ["qty.ask"]
    after = chat.session()
    assert (after.state, after.pending) == (before.state, before.pending)
    assert cart_of(chat) == {}


def test_ack_is_ignored_while_a_cart_confirmation_is_pending(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    session = chat.session()
    session.pending = {"kind": "llm_confirm", "action": "add", "items": []}
    chat.engine.store.save(WA_ID, session)
    chat.send("ok")
    assert "closing.ack" not in chat.keys

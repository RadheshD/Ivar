"""Transcripts: checkout, idempotency, rejections, payment pending -> paid / failed."""

from dataclasses import replace

import pytest

from apps.conversations.backend_port import CheckoutRejected
from apps.conversations.fake_backend import FAKE_PAYMENT_URL

from .harness import (
    ADDRESS,
    LANGUAGES,
    OUTAGE,
    PHRASES,
    WA_ID,
    checkout_to_payment,
    in_script,
    onboard,
    text_for,
)


def orders_of(backend):
    customer = backend.get_or_create_customer(WA_ID)
    return backend.list_recent_orders(customer.id, limit=50)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_checkout(chat, backend, lang):
    onboard(chat, lang=lang)
    order_number = checkout_to_payment(chat, lang)
    assert order_number == "IVA1001"
    body = chat.body
    assert FAKE_PAYMENT_URL.format(order_number=order_number) in body
    assert "₹398" in body
    # Payment is only ever described with the status the port returned.
    assert text_for(lang, "status.payment.PENDING") in body
    assert text_for(lang, "status.payment.PAID") not in body
    assert in_script(body.split("\n")[0].replace(order_number, ""), lang)
    assert chat.ids[:2] == [f"PAYMENT_STATUS:{order_number}", f"ORDER_STATUS:{order_number}"]
    [order] = orders_of(backend)
    assert (order.order_status, order.payment_status) == ("PENDING", "PENDING")


def test_idempotency_key_is_created_once_and_reused(chat, backend):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    key = chat.session().idempotency_key
    assert key
    chat.tap("ACT:confirm")
    first = chat.session().order_number
    chat.tap("ACT:confirm")  # a repeated tap or a redelivered message
    assert chat.session().order_number == first
    assert chat.session().idempotency_key == key
    assert len(orders_of(backend)) == 1


def test_lost_checkout_response_is_retried_with_the_same_key(chat, flaky, backend):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    key = chat.session().idempotency_key
    flaky.fail_after["checkout"] = OUTAGE  # the order is created, the reply is lost
    flaky.times["checkout"] = 1
    chat.tap("ACT:confirm")
    assert chat.keys == ["error.unavailable"]
    assert chat.session().idempotency_key == key  # session kept

    chat.tap("ACT:confirm")
    assert chat.keys == ["checkout.created"]
    assert len(orders_of(backend)) == 1


def test_changing_the_cart_starts_a_new_checkout_attempt(chat):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    key = chat.session().idempotency_key
    chat.send("add 1 makhana").send("checkout")
    chat.tap(chat.ids[0])  # the saved address
    assert chat.session().idempotency_key not in (None, key)


def test_saved_address_is_offered(chat):
    onboard(chat)
    checkout_to_payment(chat)
    chat.send("add 1 makhana").send("checkout")
    assert chat.keys == ["checkout.ask_address"]
    assert chat.rows[0].description == ADDRESS
    assert chat.ids[-1] == "ACT:newaddr"


def test_short_address_is_refused(chat):
    onboard(chat)
    chat.send("add 1 makhana").send("checkout").send("home")
    assert chat.keys == ["checkout.address_invalid"]


def test_menu_word_leaves_the_address_step(chat):
    onboard(chat)
    chat.send("add 1 makhana").send("checkout").send("menu")
    assert chat.keys == ["menu.body"]


def test_checkout_with_empty_cart(chat):
    onboard(chat)
    chat.send("checkout")
    assert chat.keys == ["cart.empty"]


def test_cancel_checkout(chat, backend):
    onboard(chat)
    chat.send("add 1 makhana").send("checkout").send(ADDRESS)
    chat.tap("ACT:cancel")
    assert chat.keys == ["checkout.cancelled", "menu.body"]
    assert orders_of(backend) == []


def test_price_changed_reshows_the_cart_with_current_prices(chat, flaky, backend):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    backend.set_price("makhana-crunch", "210")
    flaky.fail["checkout"] = CheckoutRejected(CheckoutRejected.PRICE_CHANGED)
    flaky.times["checkout"] = 1
    chat.tap("ACT:confirm")
    assert chat.keys == ["checkout.price_changed", "cart.title", "cart.subtotal"]
    assert "₹420" in chat.body  # 2 × the new price, from the port
    assert chat.session().idempotency_key is None
    assert "CHECKOUT" in chat.ids

    chat.send("checkout")
    chat.tap(chat.ids[0])
    chat.tap("ACT:confirm")
    assert chat.keys == ["checkout.created"]


@pytest.mark.parametrize(
    ("reason", "key"),
    [
        (CheckoutRejected.PRODUCT_UNAVAILABLE, "checkout.product_unavailable"),
        (CheckoutRejected.BELOW_MINIMUM_ORDER, "checkout.below_minimum"),
        (CheckoutRejected.OTHER, "checkout.rejected"),
        ("something_new", "checkout.rejected"),
    ],
)
def test_other_rejections(chat, flaky, reason, key):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    flaky.fail["checkout"] = CheckoutRejected(reason)
    chat.tap("ACT:confirm")
    assert chat.keys[0] == key


def test_address_not_serviceable_asks_for_another_address(chat, flaky):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    flaky.fail["checkout"] = CheckoutRejected(CheckoutRejected.ADDRESS_NOT_SERVICEABLE)
    chat.tap("ACT:confirm")
    assert chat.keys == ["checkout.not_serviceable", "checkout.ask_address"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_payment_pending_then_paid(chat, backend, lang):
    onboard(chat, lang=lang)
    order_number = checkout_to_payment(chat, lang)

    chat.send(PHRASES[lang]["payment"])
    assert chat.keys == ["payment.PENDING"]
    assert FAKE_PAYMENT_URL.format(order_number=order_number) in chat.body
    assert text_for(lang, "payment.PAID", order=order_number) not in chat.body

    backend.simulate_payment_confirmed(order_number)
    chat.send(PHRASES[lang]["payment"])
    assert chat.keys == ["payment.PAID"]
    assert chat.body.startswith(text_for(lang, "payment.PAID", order=order_number))
    assert chat.session().state == "MAIN_MENU"

    chat.send(PHRASES[lang]["order"])
    assert text_for(lang, "status.order.CONFIRMED") in chat.body


@pytest.mark.parametrize("lang", LANGUAGES)
def test_payment_failed(chat, backend, lang):
    onboard(chat, lang=lang)
    order_number = checkout_to_payment(chat, lang)
    backend.simulate_payment_failed(order_number)
    chat.tap(f"PAYMENT_STATUS:{order_number}")
    assert chat.keys == ["payment.FAILED"]
    assert FAKE_PAYMENT_URL.format(order_number=order_number) in chat.body
    assert chat.session().state == "AWAITING_PAYMENT"


def test_missing_payment_link_is_reported_not_invented(chat, backend, monkeypatch):
    onboard(chat)
    chat.send("add 2 makhana").send("checkout").send(ADDRESS)
    real_checkout = backend.checkout
    monkeypatch.setattr(
        backend, "checkout", lambda *a, **k: replace(real_checkout(*a, **k), payment_url=None)
    )
    chat.tap("ACT:confirm")
    assert text_for("en", "checkout.no_link") in chat.body
    assert "http" not in chat.body

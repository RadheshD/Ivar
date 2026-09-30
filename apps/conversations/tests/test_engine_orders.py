"""Transcripts: order status and tracking show only what the port returns."""

from datetime import UTC, datetime

import pytest
from django.utils import timezone

from .harness import OTHER_WA_ID, PHRASES, Chat, checkout_to_payment, onboard, text_for

LANGS = ("en", "te", "hi")


@pytest.mark.parametrize("lang", LANGS)
def test_order_status_without_eta_or_tracking(chat, lang):
    onboard(chat, lang=lang)
    order_number = checkout_to_payment(chat, lang)
    chat.send(PHRASES[lang]["order"])
    assert chat.keys == ["order.status"]
    body = chat.body
    assert order_number in body
    assert text_for(lang, "order.eta_na") in body
    assert text_for(lang, "order.tracking_na") in body


@pytest.mark.parametrize("lang", LANGS)
def test_order_status_with_eta_and_tracking(chat, backend, lang):
    onboard(chat, lang=lang)
    order_number = checkout_to_payment(chat, lang)
    start = datetime(2026, 9, 25, 8, 30, tzinfo=UTC)
    end = datetime(2026, 9, 25, 9, 30, tzinfo=UTC)
    backend.simulate_payment_confirmed(order_number)
    backend.simulate_status(
        order_number, "OUT_FOR_DELIVERY", eta_start=start, eta_end=end, tracking_id="TRK-778"
    )
    chat.send(PHRASES[lang]["track"])
    body = chat.body
    assert "TRK-778" in body
    assert timezone.localtime(start).strftime("%d-%m-%Y %H:%M") in body  # 14:00 IST
    assert "25-09-2026 14:00" in body
    assert text_for(lang, "status.order.OUT_FOR_DELIVERY") in body
    assert text_for(lang, "order.eta_na") not in body
    assert text_for(lang, "order.tracking_na") not in body


def test_no_orders_yet(chat):
    onboard(chat)
    chat.send("where is my order")
    assert chat.keys == ["order.none"]


def test_unknown_order_number(chat):
    onboard(chat)
    chat.send("status of IVA9999")
    assert chat.keys == ["order.not_found"]
    assert "IVA9999" in chat.body


def test_another_customers_order_is_not_revealed(engine):
    owner = onboard(Chat(engine))
    order_number = checkout_to_payment(owner)
    other = onboard(Chat(engine, OTHER_WA_ID), name="Asha")
    other.send(f"status of {order_number}")
    assert other.keys == ["order.not_found"]
    other.tap(f"PAYMENT_STATUS:{order_number}")
    assert other.keys == ["order.not_found"]


def test_order_status_by_tapping(chat):
    onboard(chat)
    order_number = checkout_to_payment(chat)
    chat.tap(f"ORDER_STATUS:{order_number}")
    assert chat.keys == ["order.status"]

"""Native catalog orders through the real signed webhook (inline mode) and the parser.

Simulated payloads only; the outbound WhatsApp client is a fake transport.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from apps.conversations.engine import get_engine, reset_engine
from apps.conversations.models import ConversationMessage
from apps.whatsapp import tasks
from apps.whatsapp.client import WhatsAppClient
from apps.whatsapp.management.commands import replay_order
from apps.whatsapp.parser import parse_webhook

from .fakes import (
    PHONE_NUMBER_ID,
    SETTINGS,
    TOKEN,
    WA_ID,
    FakeTransport,
    encode,
    payload,
    sign,
)

pytestmark = pytest.mark.django_db


def order_message(items, message_id="wamid.ORDER1", catalog_id="catalog-1", wa_id=WA_ID, **extra):
    return {
        "from": wa_id,
        "id": message_id,
        "timestamp": "1760000000",
        "type": "order",
        "order": {"catalog_id": catalog_id, "text": "", "product_items": items, **extra},
    }


def line(slug, quantity=1, price=249, currency="INR"):
    return {
        "product_retailer_id": slug,
        "quantity": quantity,
        "item_price": price,
        "currency": currency,
    }


def signed_post(data):
    body = encode(data)
    return Client().post(
        "/webhooks/whatsapp/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=sign(body),
    )


@pytest.fixture
def fake_meta(monkeypatch):
    transport = FakeTransport()

    def client():
        return WhatsAppClient(
            access_token=TOKEN,
            phone_number_id=PHONE_NUMBER_ID,
            api_version="v21.0",
            base_url="https://graph.example.invalid",
            transport=transport,
            sleep=lambda _s: None,
        )

    monkeypatch.setattr(tasks, "client_from_settings", client)
    reset_engine()
    with override_settings(**SETTINGS, IVA_INLINE_PROCESSING=True):
        yield transport
    reset_engine()


def cart_lines():
    backend = get_engine().backend
    customer = backend.get_or_create_customer(WA_ID)
    return {ln.product_id: ln.quantity for ln in backend.get_cart(customer.id).lines}


def onboard_customer():
    backend = get_engine().backend
    customer = backend.get_or_create_customer(WA_ID)
    backend.update_profile(customer.id, language="en", onboarding_complete=True)


# ---------------------------------------------------------------- parser


def test_parser_normalises_an_order():
    msg = order_message([line("ivar-oats", 2, 249.5), line("ragi-mix", "3", None)], extra="x")
    parsed = parse_webhook(payload([msg]))
    (message,) = parsed.messages
    assert message.message_type == "order"
    assert message.text is None
    assert message.order == {
        "catalog_id": "catalog-1",
        "text": None,
        "items": [
            {"product_id": "ivar-oats", "quantity": 2, "price": 249.5, "currency": "INR"},
            {"product_id": "ragi-mix", "quantity": "3", "price": None, "currency": "INR"},
        ],
        "unreadable": 0,
    }


@pytest.mark.parametrize(
    "order",
    [None, "nope", [], {}, {"product_items": "x"}, {"product_items": [1, None, {}]}],
)
def test_parser_survives_malformed_orders(order):
    msg = order_message([])
    msg["order"] = order
    (message,) = parse_webhook(payload([msg])).messages
    assert message.message_type == "order"
    assert message.order["items"] == []


def test_parser_counts_unreadable_items_and_caps_the_list():
    items = [line("ivar-oats"), {"quantity": 1}, "junk"] + [line(f"p{i}") for i in range(150)]
    (message,) = parse_webhook(payload([order_message(items)])).messages
    assert len(message.order["items"]) == 100
    assert message.order["unreadable"] == 2 + (151 - 100)


def test_parser_never_keeps_nested_values():
    items = [{"product_retailer_id": "ivar-oats", "quantity": {"a": 1}, "item_price": [1]}]
    (message,) = parse_webhook(payload([order_message(items)])).messages
    assert message.order["items"][0]["quantity"] is None
    assert message.order["items"][0]["price"] is None


# ---------------------------------------------------------------- webhook path


def test_order_through_the_webhook_fills_the_cart_and_replies(fake_meta):
    onboard_customer()
    response = signed_post(payload([order_message([line("ivar-oats", 2, 1)])]))
    assert response.status_code == 200
    assert cart_lines() == {"ivar-oats": 2}
    row = ConversationMessage.objects.get(wa_message_id="wamid.ORDER1")
    assert row.message_type == "order" and row.processing_completed_at is not None
    assert len(fake_meta.requests) >= 2  # replaced notice, price notice, cart summary...


def test_redelivered_order_changes_nothing_and_replies_once(fake_meta):
    onboard_customer()
    data = payload([order_message([line("ivar-oats", 2)])])
    signed_post(data)
    sent = len(fake_meta.requests)
    assert sent > 0
    # The customer edits the cart, then Meta redelivers the same order message.
    backend = get_engine().backend
    customer = backend.get_or_create_customer(WA_ID)
    (cart_line,) = backend.get_cart(customer.id).lines
    backend.update_quantity(customer.id, cart_line.line_id, 7)
    response = signed_post(data)
    assert response.status_code == 200
    assert cart_lines() == {"ivar-oats": 7}
    assert len(fake_meta.requests) == sent
    assert ConversationMessage.objects.filter(wa_message_id="wamid.ORDER1").count() == 1


def test_malformed_order_through_the_webhook_gets_a_safe_reply(fake_meta):
    onboard_customer()
    msg = order_message([])
    msg["order"] = "garbage"
    response = signed_post(payload([msg]))
    assert response.status_code == 200
    assert cart_lines() == {}
    assert len(fake_meta.requests) == 1
    assert "couldn" in str(fake_meta.requests[0]["json"])


def test_unsigned_order_is_refused(fake_meta):
    onboard_customer()
    response = Client().post(
        "/webhooks/whatsapp/",
        data=encode(payload([order_message([line("ivar-oats")])])),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert cart_lines() == {}


# ---------------------------------------------------------------- replay_order


def test_replay_refuses_outside_dev_mode(fake_meta):
    with override_settings(IVA_DEV_RESET_ALLOWED=False):
        with pytest.raises(CommandError, match="development"):
            call_command("replay_order", "ivar-oats:1", to=WA_ID)
    assert not ConversationMessage.objects.exists()


@override_settings(IVA_DEV_RESET_ALLOWED=True)
def test_replay_goes_through_the_signed_webhook(fake_meta, capsys, monkeypatch):
    onboard_customer()
    monkeypatch.delenv(replay_order.WA_ID_ENV, raising=False)
    call_command("replay_order", "ivar-oats:2", "ragi-mix:1:50", to=WA_ID, message_id="wamid.R1")
    out = capsys.readouterr().out
    assert "webhook status 200" in out
    assert WA_ID not in out and SETTINGS["WHATSAPP_APP_SECRET"] not in out
    assert cart_lines() == {"ivar-oats": 2, "ragi-mix": 1}
    # Same message id again: a redelivery, ignored.
    call_command("replay_order", "ivar-oats:9", to=WA_ID, message_id="wamid.R1")
    assert cart_lines() == {"ivar-oats": 2, "ragi-mix": 1}


@override_settings(IVA_DEV_RESET_ALLOWED=True)
def test_replay_rejects_bad_items_and_missing_target(fake_meta, monkeypatch):
    monkeypatch.delenv(replay_order.WA_ID_ENV, raising=False)
    with pytest.raises(CommandError, match="Bad item"):
        call_command("replay_order", "ivar-oats", to=WA_ID)
    with pytest.raises(CommandError, match="--to"):
        call_command("replay_order", "ivar-oats:1")


@override_settings(IVA_DEV_RESET_ALLOWED=True, WHATSAPP_APP_SECRET="")
def test_replay_needs_the_app_secret(fake_meta):
    with pytest.raises(CommandError, match="WHATSAPP_APP_SECRET"):
        call_command("replay_order", "ivar-oats:1", to=WA_ID)

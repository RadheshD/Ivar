"""Webhook view without a database: services and the queue are replaced by recorders."""

import logging
from types import SimpleNamespace

import pytest
from django.test import Client, override_settings

from apps.whatsapp import tasks, views

from .fakes import (
    SETTINGS,
    VERIFY_TOKEN,
    WA_ID,
    encode,
    payload,
    sign,
    status,
    text_message,
)

URL = "/webhooks/whatsapp/"


@pytest.fixture(autouse=True)
def whatsapp_settings():
    with override_settings(**SETTINGS):
        yield


@pytest.fixture
def recorded(monkeypatch):
    """Replace the database and the queue with in-memory recorders."""
    state = SimpleNamespace(seen=set(), enqueued=[], statuses=[], forgotten=[], fail_enqueue=None)

    def record_inbound(message):
        created = message.wa_message_id not in state.seen
        state.seen.add(message.wa_message_id)
        return SimpleNamespace(pk=len(state.seen)), created

    def enqueue(pk, message):
        if state.fail_enqueue:
            raise state.fail_enqueue
        state.enqueued.append((pk, message))

    monkeypatch.setattr(views.services, "record_inbound", record_inbound)
    monkeypatch.setattr(views.services, "record_status", state.statuses.append)
    monkeypatch.setattr(views.services, "forget_inbound", state.forgotten.append)
    monkeypatch.setattr(views, "enqueue", enqueue)
    return state


def post(body: bytes, signature: str | None = "auto"):
    headers = {}
    if signature == "auto":
        headers["HTTP_X_HUB_SIGNATURE_256"] = sign(body)
    elif signature is not None:
        headers["HTTP_X_HUB_SIGNATURE_256"] = signature
    return Client().post(URL, data=body, content_type="application/json", **headers)


# --------------------------------------------------------------------------- GET verification


def test_subscription_verification():
    response = Client().get(
        URL, {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"}
    )
    assert response.status_code == 200
    assert response.content == b"12345"
    assert response["Content-Type"].startswith("text/plain")


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "<script>"},
        {},
    ],
)
def test_subscription_verification_refused(params):
    assert Client().get(URL, params).status_code == 403


def test_verification_refused_when_token_not_configured():
    with override_settings(WHATSAPP_VERIFY_TOKEN=""):
        response = Client().get(
            URL, {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}
        )
    assert response.status_code == 403


def test_other_methods_are_not_allowed():
    assert Client().put(URL).status_code == 405


# --------------------------------------------------------------------------- POST


@pytest.mark.parametrize("signature", [None, "sha256=0000", sign(b"other body")])
def test_invalid_signature_is_403_and_nothing_is_processed(recorded, signature):
    response = post(encode(payload([text_message()])), signature)
    assert response.status_code == 403
    assert recorded.seen == set() and recorded.enqueued == []


def test_missing_app_secret_refuses_everything(recorded):
    with override_settings(WHATSAPP_APP_SECRET=""):
        response = post(encode(payload([text_message()])), sign(b"x", ""))
    assert response.status_code == 403
    assert recorded.enqueued == []


def test_valid_message_is_recorded_and_queued(recorded):
    response = post(encode(payload([text_message("show my cart")])))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    [(pk, message)] = recorded.enqueued
    assert message.text == "show my cart"
    assert message.wa_id == WA_ID


def test_duplicate_delivery_is_acknowledged_and_ignored(recorded):
    body = encode(payload([text_message(message_id="wamid.DUP")]))
    assert post(body).status_code == 200
    assert post(body).status_code == 200
    assert len(recorded.enqueued) == 1


def test_status_callbacks_are_recorded_never_replied_to(recorded):
    response = post(encode(payload(statuses=[status("wamid.OUT1", "read")])))
    assert response.status_code == 200
    assert [s.status for s in recorded.statuses] == ["read"]
    assert recorded.enqueued == []


def test_queue_failure_forgets_the_message_and_asks_meta_to_retry(recorded):
    recorded.fail_enqueue = ConnectionError("broker down")
    response = post(encode(payload([text_message()])))
    assert response.status_code == 503
    assert recorded.forgotten == [1]


def test_invalid_json_with_a_valid_signature(recorded):
    assert post(b"not json").status_code == 400


def test_other_phone_number_is_ignored(recorded):
    response = post(encode(payload([text_message()], phone_number_id="999")))
    assert response.status_code == 200
    assert recorded.enqueued == []


def test_without_a_database_the_webhook_asks_meta_to_retry():
    # Real services, but no DATABASE_URL: recording fails, so 503 (Meta retries later).
    response = post(encode(payload([text_message()])))
    assert response.status_code == 503


def test_webhook_logs_carry_no_text_or_number(recorded, caplog):
    body = encode(payload([text_message("my address is Flat 12 Green Park")]))
    with caplog.at_level(logging.DEBUG):
        post(body)
        post(body)  # duplicate
        post(body, "sha256=0000")  # invalid signature
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Flat 12" not in logged
    assert WA_ID not in logged


def test_enqueue_masks_task_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.process_inbound, "apply_async", lambda **kw: calls.append(kw), raising=False
    )
    from apps.whatsapp.parser import parse_webhook

    [message] = parse_webhook(payload([text_message("secret text")])).messages
    tasks.enqueue(7, message)
    [call] = calls
    assert call["kwargs"]["wa_id"] == WA_ID  # the worker needs the real number to reply
    assert WA_ID not in call["kwargsrepr"]
    assert "secret text" not in call["kwargsrepr"]
    assert "91******0001" in call["kwargsrepr"]

"""Order-status notifications pushed by the backend: the authenticated endpoint, idempotency,
the 24-hour window, templates, opt-out, language, the simulate command and log privacy.

Simulated only: the WhatsApp client is a fake transport, nothing reaches Meta, and no real
template has been approved (the template names below are made up for the tests).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from apps.conversations import order_updates
from apps.conversations.adapters import reset_backend
from apps.conversations.backend_port import BackendUnavailable
from apps.conversations.engine import get_engine, reset_engine
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.messages import t
from apps.conversations.models import Conversation, ConversationMessage, OrderEventRecord
from apps.whatsapp import order_events, services, tasks, views
from apps.whatsapp.client import WhatsAppClient

from .fakes import (
    PHONE_NUMBER_ID,
    SETTINGS,
    TOKEN,
    WA_ID,
    FakeTransport,
    error,
)

pytestmark = pytest.mark.django_db

SECRET = "events-secret-marker-xyz"
NAME = "Ravi Kumar"
ADDRESS = "Flat 12, Green Park Road, Madhapur, Hyderabad 500081"
Outcome = OrderEventRecord.Outcome
URL = "/webhooks/whatsapp/order-events/"
TEMPLATES = {
    "CONFIRMED": {
        "en": "order_confirmed_en",
        "hi": "order_confirmed_hi",
        "te": "order_confirmed_te",
    },
    "DELIVERED": {"en": "order_delivered_en"},
}


@pytest.fixture
def meta(monkeypatch):
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
    reset_backend()
    reset_engine()
    with override_settings(**SETTINGS, IVA_INLINE_PROCESSING=True, IVA_ORDER_EVENTS_SECRET=SECRET):
        yield transport
    reset_engine()
    reset_backend()


def make_order(language="en", status="CONFIRMED", backend=None):
    """A customer with a placed order, put in ``status`` (so order replies match the event)."""
    backend = backend or get_engine().backend
    customer = backend.get_or_create_customer(WA_ID)
    backend.update_profile(customer.id, language=language, name=NAME, onboarding_complete=True)
    backend.add_item(customer.id, "ivar-oats--std", 1)
    address = backend.add_address(customer.id, ADDRESS)
    order = backend.checkout(customer.id, address.id, uuid.uuid4().hex).order_number
    backend.simulate_status(order, status)
    return customer, order


def window(inside):
    """``True``: the customer wrote a minute ago. ``False``: 25 hours ago. ``None``: never."""
    conversation = services.conversation_for(WA_ID)
    if inside is None:
        return
    when = timezone.now() - (timedelta(minutes=1) if inside else timedelta(hours=25))
    Conversation.objects.filter(pk=conversation.pk).update(last_inbound_at=when)


def body_for(order, customer, status="CONFIRMED", event_id="evt-1"):
    return {
        "event_id": event_id,
        "order_number": order,
        "customer_id": customer.id,
        "new_status": status,
        "occurred_at": timezone.now().isoformat(),
    }


def post(data=None, *, secret=SECRET, raw=None):
    headers = {} if secret is None else {"HTTP_X_IVA_EVENTS_SECRET": secret}
    body = raw if raw is not None else json.dumps(data).encode()
    return Client().post(URL, data=body, content_type="application/json", **headers)


def push(order, customer, status="CONFIRMED", event_id="evt-1"):
    return post(body_for(order, customer, status, event_id))


def outcome(event_id="evt-1"):
    return OrderEventRecord.objects.get(event_id=event_id).outcome


def sent_payloads(meta):
    return [r["json"] for r in meta.requests]


# ----------------------------------------------------------------------- authentication


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x", SECRET.upper()])
def test_a_missing_or_wrong_secret_is_refused_with_no_detail(meta, secret):
    customer, order = make_order()
    window(True)
    response = post(body_for(order, customer), secret=secret)
    assert response.status_code == 403
    assert response.json() == {"status": "rejected"}
    assert not OrderEventRecord.objects.exists()  # nothing recorded
    assert meta.requests == []  # nothing sent


def test_no_configured_secret_means_everything_is_refused(meta):
    customer, order = make_order()
    with override_settings(IVA_ORDER_EVENTS_SECRET=""):
        for secret in (None, "", "anything"):
            assert post(body_for(order, customer), secret=secret).status_code == 403
    assert meta.requests == []


def test_the_secret_is_compared_in_constant_time(meta, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        "apps.whatsapp.security.hmac.compare_digest",
        lambda a, b: calls.append(1) or real(a, b),
    )
    customer, order = make_order()
    post(body_for(order, customer), secret="wrong")
    assert calls


def test_only_post_is_allowed(meta):
    assert Client().get(URL, HTTP_X_IVA_EVENTS_SECRET=SECRET).status_code == 405


def test_authentication_comes_before_any_parsing(meta):
    response = post(raw=b"{not json", secret="wrong")
    assert response.status_code == 403  # not 400: a stranger learns nothing about the format


# ----------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'"text"',
        b"null",
        b"{}",
        b"[" * 3000,  # nesting deep enough to exhaust the recursion limit
        b'{"event_id": "e"}',
        b"x" * 5000,  # over the size cap
    ],
)
def test_a_malformed_body_is_refused_with_no_detail(meta, raw):
    response = post(raw=raw)
    assert response.status_code == 400
    assert response.json() == {"status": "rejected"}
    assert not OrderEventRecord.objects.exists()
    assert meta.requests == []


def test_an_invalid_field_is_refused(meta):
    customer, order = make_order()
    for changes in ({"new_status": "SHIPPED"}, {"occurred_at": "nope"}, {"event_id": "a b"}):
        data = {**body_for(order, customer), **changes}
        assert post(data).status_code == 400
    assert not OrderEventRecord.objects.exists()


def test_a_refusal_never_echoes_the_request(meta):
    customer, order = make_order()
    response = post({**body_for(order, customer), "new_status": "SECRET-WORD"})
    assert b"SECRET-WORD" not in response.content and order.encode() not in response.content


# ----------------------------------------------------------------------- declared length


class BodyWasRead(AssertionError):
    pass


@pytest.fixture
def unreadable_body():
    """Any read of ``request.body`` fails the test: proves a refusal happened before it."""
    from unittest.mock import PropertyMock, patch

    with patch("django.core.handlers.wsgi.WSGIRequest.body", new_callable=PropertyMock) as body:
        body.side_effect = BodyWasRead("the body was read")
        yield body


def direct_request(body: bytes, *, content_length="auto", secret=SECRET):
    """Call the view directly so the declared length can be set or removed at will."""
    from django.test import RequestFactory

    headers = {} if secret is None else {"HTTP_X_IVA_EVENTS_SECRET": secret}
    request = RequestFactory().post(URL, data=body, content_type="application/json", **headers)
    if content_length is None:
        request.META.pop("CONTENT_LENGTH", None)
    elif content_length != "auto":
        request.META["CONTENT_LENGTH"] = content_length
    return views.order_status_events(request)


def test_an_oversized_declared_length_is_refused_before_the_body_is_read(meta, unreadable_body):
    response = direct_request(b"{}", content_length=str(views.MAX_EVENT_BODY_BYTES + 1))
    assert response.status_code == 400
    assert json.loads(response.content) == {"status": "rejected"}
    assert not OrderEventRecord.objects.exists() and meta.requests == []


def test_a_missing_declared_length_is_refused_before_the_body_is_read(meta, unreadable_body):
    response = direct_request(b"{}", content_length=None)
    assert response.status_code == 400
    assert json.loads(response.content) == {"status": "rejected"}
    assert not OrderEventRecord.objects.exists()


@pytest.mark.parametrize(
    "declared", ["", " ", "abc", "-1", "1e3", "12.5", "٤٠٩٦", "99999999999999"]
)
def test_an_invalid_declared_length_is_refused_before_the_body_is_read(
    meta, unreadable_body, declared
):
    assert direct_request(b"{}", content_length=declared).status_code == 400


def test_authentication_still_comes_before_the_length_check(meta, unreadable_body):
    response = direct_request(b"{}", content_length=None, secret="wrong")
    assert response.status_code == 403  # a stranger learns nothing about the length rule


def test_the_declared_length_limit_is_inclusive(meta):
    at_limit = str(views.MAX_EVENT_BODY_BYTES)
    assert views._declared_length_ok(type("R", (), {"META": {"CONTENT_LENGTH": at_limit}})())
    over = str(views.MAX_EVENT_BODY_BYTES + 1)
    assert not views._declared_length_ok(type("R", (), {"META": {"CONTENT_LENGTH": over}})())


def test_a_valid_small_event_with_a_content_length_is_accepted(meta):
    customer, order = make_order()
    window(True)
    body = json.dumps(body_for(order, customer)).encode()
    assert 0 < len(body) <= views.MAX_EVENT_BODY_BYTES
    response = direct_request(body)  # the test client sets Content-Length from the body
    assert (response.status_code, json.loads(response.content)) == (200, {"status": "accepted"})
    assert outcome() == Outcome.SENT_FREE_FORM


def test_the_post_read_size_check_remains_as_a_backstop(meta):
    """Over WSGI a body cannot exceed its declared length, so fake one that does (a proxy or
    server quirk): the length of the bytes actually read is still checked."""
    from unittest.mock import PropertyMock, patch

    customer, order = make_order()
    small = json.dumps(body_for(order, customer)).encode()
    oversized = small + b" " * views.MAX_EVENT_BODY_BYTES  # valid JSON, too long
    with patch("django.core.handlers.wsgi.WSGIRequest.body", new_callable=PropertyMock) as body:
        body.return_value = oversized
        response = direct_request(small)  # the declared length is small, so it passes
    assert response.status_code == 400
    assert json.loads(response.content) == {"status": "rejected"}
    assert not OrderEventRecord.objects.exists() and meta.requests == []


# ----------------------------------------------------------------------- inside the window


@pytest.mark.parametrize("lang", ["en", "hi", "te"])
def test_inside_the_window_a_free_form_message_in_the_customers_language(meta, lang):
    customer, order = make_order(lang)
    window(True)
    response = push(order, customer)
    assert (response.status_code, response.json()) == (200, {"status": "accepted"})
    (payload,) = sent_payloads(meta)
    assert payload["type"] == "text" and payload["to"] == WA_ID
    expected = (
        t(lang, "notify.order_status.CONFIRMED", order=order) + "\n\n" + t(lang, "notify.footer")
    )
    assert payload["text"]["body"] == expected
    assert outcome() == Outcome.SENT_FREE_FORM
    row = ConversationMessage.objects.get(direction="out")
    assert row.message_type == "text" and row.wa_message_id


@pytest.mark.parametrize(
    "status",
    ["PENDING", "CONFIRMED", "PROCESSING", "PACKED", "OUT_FOR_DELIVERY", "DELIVERED", "CANCELLED"],
)
def test_every_status_the_fake_backend_supports_is_worded(meta, status):
    customer, order = make_order(status=status)
    window(True)
    push(order, customer, status)
    (payload,) = sent_payloads(meta)
    assert t("en", f"notify.order_status.{status}", order=order) in payload["text"]["body"]


def test_the_message_states_nothing_beyond_the_event(meta):
    customer, order = make_order(status="OUT_FOR_DELIVERY")
    get_engine().backend.simulate_status(
        order, "OUT_FOR_DELIVERY", tracking_id="TRK-SECRET-1", eta_start=timezone.now()
    )  # the order has an ETA and tracking ID: the notification must not volunteer them
    window(True)
    push(order, customer, "OUT_FOR_DELIVERY")
    body = sent_payloads(meta)[0]["text"]["body"]
    status_line = t("en", "notify.order_status.OUT_FOR_DELIVERY", order=order)
    assert body == status_line + "\n\n" + t("en", "notify.footer")  # nothing else at all
    assert "TRK-SECRET-1" not in body
    assert not [d for d in re.findall(r"\d+", body) if d not in order]  # no invented times


def test_an_unset_language_uses_english(meta):
    customer, order = make_order()
    get_engine().backend.update_profile(customer.id, language="en")
    window(True)
    push(order, customer)
    assert "is confirmed" in sent_payloads(meta)[0]["text"]["body"]


# ----------------------------------------------------------------------- outside the window


@pytest.mark.parametrize("inside", [False, None])
def test_outside_the_window_with_a_template_configured_it_sends_that_template(meta, inside):
    customer, order = make_order("hi")
    window(inside)  # 25 hours ago, or never written at all
    with override_settings(IVA_ORDER_STATUS_TEMPLATES=TEMPLATES):
        push(order, customer)
    (payload,) = sent_payloads(meta)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "order_confirmed_hi"
    assert payload["template"]["language"] == {"code": "hi"}
    assert payload["template"]["components"][0]["parameters"] == [{"type": "text", "text": order}]
    assert outcome() == Outcome.SENT_TEMPLATE
    assert ConversationMessage.objects.get(direction="out").message_type == "template"


def test_outside_the_window_with_no_template_nothing_is_sent_and_it_is_recorded(meta, caplog):
    customer, order = make_order()
    window(False)
    with caplog.at_level(logging.WARNING):
        response = push(order, customer)
    assert response.status_code == 200
    assert meta.requests == []
    assert outcome() == Outcome.UNDELIVERED_NO_TEMPLATE
    assert any("no_template" in r.getMessage() for r in caplog.records)
    assert not ConversationMessage.objects.filter(direction="out").exists()


@pytest.mark.parametrize(
    "templates",
    [
        {},
        {"CONFIRMED": {}},
        {"DELIVERED": {"en": "order_delivered_en"}},  # another status
        {"CONFIRMED": {"hi": "order_confirmed_hi"}},  # another language: never a fallback
    ],
)
def test_a_template_for_another_status_or_language_is_never_substituted(meta, templates):
    customer, order = make_order("en")
    window(False)
    with override_settings(IVA_ORDER_STATUS_TEMPLATES=templates):
        push(order, customer)
    assert meta.requests == []
    assert outcome() == Outcome.UNDELIVERED_NO_TEMPLATE


def test_the_window_boundary_is_twenty_four_hours(meta):
    customer, order = make_order()
    conversation = services.conversation_for(WA_ID)
    just_inside = timezone.now() - timedelta(hours=23, minutes=59)
    Conversation.objects.filter(pk=conversation.pk).update(last_inbound_at=just_inside)
    push(order, customer)
    assert sent_payloads(meta)[0]["type"] == "text"


def test_template_names_come_only_from_settings():
    assert order_events.template_name("CONFIRMED", "en") is None
    with override_settings(IVA_ORDER_STATUS_TEMPLATES=TEMPLATES):
        assert order_events.template_name("CONFIRMED", "te") == "order_confirmed_te"
        assert order_events.template_name("PACKED", "en") is None


# ----------------------------------------------------------------------- idempotency


def test_the_same_event_id_never_notifies_twice(meta):
    customer, order = make_order()
    window(True)
    first = push(order, customer)
    second = push(order, customer)
    third = push(order, customer)
    assert first.json() == {"status": "accepted"}
    assert second.json() == third.json() == {"status": "duplicate"}
    assert second.status_code == 200  # acknowledged, so the backend stops retrying
    assert len(meta.requests) == 1
    assert OrderEventRecord.objects.filter(event_id="evt-1").count() == 1


def test_a_different_event_id_for_the_same_status_is_a_new_event(meta):
    customer, order = make_order()
    window(True)
    push(order, customer, event_id="evt-1")
    push(order, customer, event_id="evt-2")
    assert len(meta.requests) == 2


def test_a_duplicate_is_ignored_even_after_the_first_was_undelivered(meta):
    customer, order = make_order()
    window(False)
    push(order, customer)
    window(True)  # the customer writes later; the event must still not be re-sent
    assert push(order, customer).json() == {"status": "duplicate"}
    assert meta.requests == []


def test_event_ids_live_in_the_database_and_never_in_redis_or_the_cache():
    source = Path(order_events.__file__).read_text(encoding="utf-8")
    assert "cache" not in source and "import redis" not in source
    assert OrderEventRecord._meta.get_field("event_id").unique


def test_no_phone_number_or_name_is_stored_with_the_event(meta):
    customer, order = make_order()
    window(True)
    push(order, customer)
    record = OrderEventRecord.objects.get(event_id="evt-1")
    stored = " ".join(str(getattr(record, f.name)) for f in OrderEventRecord._meta.get_fields())
    assert WA_ID not in stored and NAME not in stored


# ----------------------------------------------------------------------- opt-out


def test_an_opted_out_customer_gets_nothing_inside_or_outside_the_window(meta):
    customer, order = make_order()
    order_updates.set_updates_enabled(WA_ID, False)
    with override_settings(IVA_ORDER_STATUS_TEMPLATES=TEMPLATES):
        for inside, event_id in ((True, "evt-1"), (False, "evt-2")):
            window(inside)
            push(order, customer, event_id=event_id)
            assert outcome(event_id) == Outcome.OPTED_OUT
    assert meta.requests == []


def test_resuming_sends_the_next_event(meta):
    customer, order = make_order()
    window(True)
    order_updates.set_updates_enabled(WA_ID, False)
    push(order, customer, event_id="evt-1")
    order_updates.set_updates_enabled(WA_ID, True)
    push(order, customer, event_id="evt-2")
    assert outcome("evt-1") == Outcome.OPTED_OUT and outcome("evt-2") == Outcome.SENT_FREE_FORM
    assert len(meta.requests) == 1


def test_the_typed_phrase_stops_the_next_notification_end_to_end(meta):
    customer, order = make_order()
    window(True)
    from apps.conversations.channel import Inbound

    get_engine().handle(WA_ID, Inbound.typed("stop order updates"))
    push(order, customer)
    assert outcome() == Outcome.OPTED_OUT and meta.requests == []
    get_engine().handle(WA_ID, Inbound.typed("resume order updates"))
    push(order, customer, event_id="evt-2")
    assert outcome("evt-2") == Outcome.SENT_FREE_FORM


# ----------------------------------------------------------------------- consistency checks


def test_an_event_the_order_has_moved_past_is_not_notified(meta):
    customer, order = make_order(status="PACKED")
    window(True)
    push(order, customer, "CONFIRMED")  # stale: the order is already PACKED
    assert outcome() == Outcome.SUPERSEDED and meta.requests == []


def test_an_unknown_customer_or_order_sends_nothing(meta):
    customer, order = make_order()
    window(True)
    post({**body_for(order, customer), "customer_id": "cust_999", "event_id": "e1"})
    post({**body_for(order, customer), "order_number": "IVA9999", "event_id": "e2"})
    assert outcome("e1") == Outcome.UNKNOWN_CUSTOMER
    assert outcome("e2") == Outcome.UNKNOWN_ORDER
    assert meta.requests == []


def test_another_customers_order_is_never_notified(meta):
    backend = get_engine().backend
    customer, order = make_order()
    other = backend.get_or_create_customer("919876500002")
    window(True)
    post({**body_for(order, customer), "customer_id": other.id})
    assert outcome() == Outcome.UNKNOWN_ORDER and meta.requests == []


def test_a_failed_whatsapp_send_is_recorded_not_raised(meta):
    meta.default = error(400)
    customer, order = make_order()
    window(True)
    assert push(order, customer).status_code == 200
    assert outcome() == Outcome.SEND_FAILED
    assert ConversationMessage.objects.get(direction="out").status == "failed"


def test_a_backend_outage_is_recorded_not_raised(meta, monkeypatch):
    customer, order = make_order()
    window(True)

    def down(*_a, **_k):
        raise BackendUnavailable("down")

    monkeypatch.setattr(get_engine().backend, "get_customer", down)
    assert push(order, customer).status_code == 200
    assert outcome() == Outcome.BACKEND_UNAVAILABLE and meta.requests == []


def test_an_unexpected_error_after_claiming_still_answers_200(meta, monkeypatch):
    customer, order = make_order()

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(views, "notify_order_status_inline", boom)
    assert push(order, customer).status_code == 200  # claimed: the backend must not redeliver


# ----------------------------------------------------------------------- worker mode


def test_with_a_worker_the_event_is_queued_with_ids_only(meta, monkeypatch):
    customer, order = make_order()
    queued = []
    monkeypatch.setattr(
        views.notify_order_status, "apply_async", lambda **kwargs: queued.append(kwargs)
    )
    with override_settings(IVA_INLINE_PROCESSING=False):
        response = push(order, customer)
    assert response.json() == {"status": "accepted"} and meta.requests == []
    (kwargs,) = queued
    assert set(kwargs["kwargs"]["fields"]) == {
        "event_id",
        "order_number",
        "customer_id",
        "new_status",
        "occurred_at",
    }
    assert WA_ID not in json.dumps(kwargs) and NAME not in json.dumps(kwargs)
    assert outcome() == Outcome.RECEIVED


def test_if_queueing_fails_the_claim_is_released_so_the_retry_is_accepted(meta, monkeypatch):
    customer, order = make_order()
    window(True)

    def broker_down(**_kwargs):
        raise OSError("broker down")

    monkeypatch.setattr(views.notify_order_status, "apply_async", broker_down)
    with override_settings(IVA_INLINE_PROCESSING=False):
        response = push(order, customer)
    assert response.status_code == 503 and response.json() == {"status": "unavailable"}
    assert not OrderEventRecord.objects.exists()
    monkeypatch.undo()
    assert push(order, customer).json() == {"status": "accepted"}  # the retry works


def test_the_celery_task_handles_an_event(meta):
    customer, order = make_order()
    window(True)
    from apps.conversations.backend_port import parse_order_status_change

    parsed = parse_order_status_change(body_for(order, customer))
    assert order_events.claim(parsed)
    tasks.notify_order_status.apply(kwargs={"fields": tasks.order_event_fields(parsed)}).get()
    assert outcome() == Outcome.SENT_FREE_FORM and len(meta.requests) == 1


def test_celery_logs_never_show_the_event_ids(meta, monkeypatch):
    customer, order = make_order()
    queued = []
    monkeypatch.setattr(
        views.notify_order_status, "apply_async", lambda **kwargs: queued.append(kwargs)
    )
    with override_settings(IVA_INLINE_PROCESSING=False):
        push(order, customer, event_id="evt-visible-check")
    (kwargs,) = queued
    shown = kwargs["kwargsrepr"]
    for private in (customer.id, order, "evt-visible-check", "CONFIRMED", WA_ID):
        assert private not in shown, private
    assert kwargs["kwargs"]["fields"]["customer_id"] == customer.id  # the task still gets them


def test_the_real_celery_message_header_is_masked_while_the_body_carries_the_fields(
    meta, monkeypatch
):
    """What Celery's worker logs for the task comes from the message header ``kwargsrepr``;
    the body (the broker payload) must still hold the fields, because the task needs them."""
    customer, order = make_order()
    queued = []
    monkeypatch.setattr(
        views.notify_order_status, "apply_async", lambda **kwargs: queued.append(kwargs)
    )
    with override_settings(IVA_INLINE_PROCESSING=False):
        push(order, customer, event_id="evt-header-check")
    (kwargs,) = queued
    message = tasks.notify_order_status.app.amqp.as_task_v2(
        "task-id-1",
        tasks.notify_order_status.name,
        kwargs=kwargs["kwargs"],
        kwargsrepr=kwargs["kwargsrepr"],
    )
    logged = message.headers["kwargsrepr"] + message.headers["argsrepr"]
    for private in (customer.id, order, "evt-header-check"):
        assert private not in logged, private
    body = json.dumps(message.body[1])  # the kwargs in the payload the broker carries
    assert order in body and "evt-header-check" in body and customer.id in body


# ----------------------------------------------------------------------- the simulate command


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "fake_backend.json"
    backend = FakeBackend(state_file=path)
    make_order(backend=backend, status="PENDING")
    old = time.time() - 600
    os.utime(path, (old, old))  # not "just written": the server-is-writing check stays quiet
    return path


@pytest.fixture
def dev(meta, state_path):
    with override_settings(
        IVA_DEV_RESET_ALLOWED=True,
        IVA_FAKE_BACKEND_STATE_FILE=str(state_path),
        IVA_BACKEND_ADAPTER="apps.conversations.fake_backend.FakeBackend",
    ):
        reset_backend()
        reset_engine()
        yield state_path


def order_number(path):
    backend = FakeBackend(state_file=path)
    return backend.get_latest_order(backend.get_or_create_customer(WA_ID).id).order_number


def stored_status(path):
    backend = FakeBackend(state_file=path)
    return backend.get_latest_order(backend.get_or_create_customer(WA_ID).id).order_status


def run(*args) -> str:
    out = StringIO()
    call_command("simulate_order_status", *args, stdout=out)
    return out.getvalue()


def age(path):
    old = time.time() - 600
    os.utime(path, (old, old))


def test_refuses_unless_dev_reset_is_allowed(dev):
    with override_settings(IVA_DEV_RESET_ALLOWED=False):
        with pytest.raises(CommandError, match="IVA_DEV_RESET_ALLOWED"):
            run(order_number(dev), "CONFIRMED")
    assert stored_status(dev) == "PENDING" and not OrderEventRecord.objects.exists()


def test_refuses_with_a_real_backend_adapter(dev):
    with override_settings(IVA_BACKEND_ADAPTER="ivar_backend.adapter.RealAdapter"):
        with pytest.raises(CommandError, match="Fake Backend"):
            run(order_number(dev), "CONFIRMED")
    assert stored_status(dev) == "PENDING"


def test_refuses_without_a_state_file(dev):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=""):
        with pytest.raises(CommandError, match="IVA_FAKE_BACKEND_STATE_FILE"):
            run("IVA1001", "CONFIRMED")


def test_refuses_without_the_events_secret(dev):
    with override_settings(IVA_ORDER_EVENTS_SECRET=""):
        with pytest.raises(CommandError, match="IVA_ORDER_EVENTS_SECRET"):
            run(order_number(dev), "CONFIRMED")
    assert stored_status(dev) == "PENDING"


def test_refuses_when_the_state_file_was_written_a_moment_ago(dev):
    now = time.time()
    os.utime(dev, (now, now))
    with pytest.raises(CommandError, match="written"):
        run(order_number(dev), "CONFIRMED")
    assert stored_status(dev) == "PENDING"


def test_an_unknown_order_or_status_is_refused(dev):
    with pytest.raises(CommandError, match="not in the dev state file"):
        run("IVA9999", "CONFIRMED")
    with pytest.raises(CommandError):
        run(order_number(dev), "SHIPPED")  # argparse choices
    assert stored_status(dev) == "PENDING"


def test_a_dry_run_changes_and_sends_nothing(dev, meta):
    window(True)
    text = run(order_number(dev), "CONFIRMED", "--dry-run")
    assert "Dry run" in text and "Nothing changed" in text
    assert stored_status(dev) == "PENDING"
    assert meta.requests == [] and not OrderEventRecord.objects.exists()


def test_it_updates_the_stored_status_and_pushes_the_event(dev, meta):
    window(True)
    order = order_number(dev)
    text = run(order, "CONFIRMED", "--event-id", "sim-1")
    age(dev)
    assert "endpoint status 200" in text
    assert stored_status(dev) == "CONFIRMED"  # order-status replies stay consistent
    assert outcome("sim-1") == Outcome.SENT_FREE_FORM
    assert (
        t("en", "notify.order_status.CONFIRMED", order=order)
        in sent_payloads(meta)[0]["text"]["body"]
    )


def test_reusing_the_event_id_is_a_duplicate_and_sends_once(dev, meta):
    window(True)
    order = order_number(dev)
    run(order, "CONFIRMED", "--event-id", "sim-1")
    age(dev)
    text = run(order, "CONFIRMED", "--event-id", "sim-1")
    assert "endpoint status 200" in text
    assert len(meta.requests) == 1


def test_a_generated_event_id_is_new_each_time(dev, meta):
    window(True)
    order = order_number(dev)
    run(order, "CONFIRMED")
    age(dev)
    run(order, "PACKED")
    assert OrderEventRecord.objects.count() == 2 and len(meta.requests) == 2


def test_the_output_never_shows_the_secret_the_whatsapp_id_or_the_path(dev):
    window(True)
    out = run(order_number(dev), "CONFIRMED") + run(order_number(dev), "PACKED", "--dry-run")
    for private in (SECRET, WA_ID, NAME, str(dev.parent), TOKEN):
        assert private not in out


# ----------------------------------------------------------------------- privacy


def test_the_notification_paths_never_log_name_address_phone_or_secret(meta, caplog):
    customer, order = make_order()
    other = get_engine().backend.get_or_create_customer("919876500002")
    with caplog.at_level(logging.DEBUG), override_settings(IVA_ORDER_STATUS_TEMPLATES=TEMPLATES):
        window(True)
        push(order, customer, "CONFIRMED", "e1")  # free-form
        push(order, customer, "CONFIRMED", "e1")  # duplicate
        window(False)
        push(order, customer, "CONFIRMED", "e2")  # template
        push(order, customer, "PACKED", "e3")  # superseded
        get_engine().backend.simulate_status(order, "PACKED")
        push(order, customer, "PACKED", "e4")  # no template
        order_updates.set_updates_enabled(WA_ID, False)
        push(order, customer, "PACKED", "e5")  # opted out
        post(body_for(order, customer), secret="wrong")  # refused
        post(raw=b"{bad")  # malformed
        post({**body_for(order, customer), "customer_id": other.id, "event_id": "e6"})
        meta.default = error(400)
        order_updates.set_updates_enabled(WA_ID, True)
        window(True)
        push(order, customer, "PACKED", "e7")  # send failure
    logged = "\n".join([r.getMessage() + (r.exc_text or "") for r in caplog.records])
    assert any("order event" in r.getMessage() for r in caplog.records)
    for private in (NAME, ADDRESS, "Green Park", WA_ID, SECRET, TOKEN, "919876500002"):
        assert private not in logged, private

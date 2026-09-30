"""A per-customer lock timeout retries instead of losing the message (S5 review fix).

The timeout is raised before the engine ever runs, so releasing the claim and
re-enqueuing is safe. ``customer_lock`` itself is replaced with a fake that times out a
controlled number of times, so these tests are deterministic and need no real Redis
contention or threads.

Every test that expects an actual retry overrides ``CELERY_TASK_EAGER_PROPAGATES`` to
``False``: with it ``True`` (the project's test-settings default, chosen so a genuine
bug's exception surfaces immediately in a failing test), Celery's eager tracer
re-raises ``Retry`` instead of recursively re-applying the task, so ``self.retry()``
would never actually retry under the suite's normal settings.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
from django.test import Client, override_settings

from apps.conversations.engine import reset_engine
from apps.conversations.models import ConversationMessage
from apps.whatsapp import services, tasks
from apps.whatsapp.client import WhatsAppClient
from apps.whatsapp.locks import CustomerLockTimeout
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
    text_message,
)

pytestmark = pytest.mark.django_db


def inbound(body="hi", message_id="wamid.IN1"):
    [message] = parse_webhook(payload([text_message(body, message_id)])).messages
    return message


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
    with override_settings(**SETTINGS):
        yield transport
    reset_engine()


def _flaky_lock(fail_times: int):
    """Stands in for ``customer_lock``: raises ``CustomerLockTimeout`` on the first
    ``fail_times`` calls (before ever yielding, exactly like a real timeout), then
    behaves like an uncontended lock. ``calls`` counts every attempt."""
    calls: list[None] = []

    @contextlib.contextmanager
    def fake_lock(_wa_id, **_kwargs):
        calls.append(None)
        if len(calls) <= fail_times:
            raise CustomerLockTimeout("simulated for test")
        yield

    return fake_lock, calls


@override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
def test_retry_succeeds_after_the_lock_frees(fake_meta, monkeypatch):
    fake_lock, calls = _flaky_lock(fail_times=2)
    monkeypatch.setattr(tasks, "customer_lock", fake_lock)

    response = signed_post(payload([text_message("hi", "wamid.RETRY_OK")]))

    assert response.status_code == 200
    assert len(calls) == 3  # 2 timeouts, then the retry that finally acquires it
    row = ConversationMessage.objects.get(wa_message_id="wamid.RETRY_OK")
    assert row.processing_completed_at is not None
    assert row.stall_reported_at is None
    assert len(fake_meta.requests) == 1  # the engine ran exactly once, on the successful attempt


@override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
def test_retries_exhausted_reports_stalled_once(fake_meta, monkeypatch, caplog):
    fake_lock, calls = _flaky_lock(fail_times=999)  # never frees
    monkeypatch.setattr(tasks, "customer_lock", fake_lock)

    with caplog.at_level(logging.WARNING):
        response = signed_post(payload([text_message("hi", "wamid.EXHAUSTED")]))

    assert response.status_code == 200
    assert len(calls) == tasks.MAX_LOCK_RETRIES + 1  # the initial attempt plus every retry
    row = ConversationMessage.objects.get(wa_message_id="wamid.EXHAUSTED")
    assert row.processing_completed_at is None
    assert row.stall_reported_at is not None
    assert len(fake_meta.requests) == 0  # the engine never ran; no reply was ever sent
    exhausted_warnings = [r for r in caplog.records if "lock retries exhausted" in r.getMessage()]
    assert len(exhausted_warnings) == 1


@override_settings(CELERY_TASK_EAGER_PROPAGATES=False)
def test_no_double_processing_after_lock_retries_exhausted(fake_meta, monkeypatch):
    fake_lock, calls = _flaky_lock(fail_times=999)
    monkeypatch.setattr(tasks, "customer_lock", fake_lock)

    row, _ = services.record_inbound(inbound("hi", "wamid.NO_DOUBLE"))
    kwargs = {
        "message_pk": row.pk,
        "wa_id": WA_ID,
        "text": "hi",
        "reply_id": None,
        "reply_title": None,
    }
    tasks.process_inbound.apply_async(kwargs=kwargs)  # exhausts retries, gets reported
    calls_after_first_delivery = len(calls)
    row.refresh_from_db()
    assert row.processing_completed_at is None
    assert row.stall_reported_at is not None

    # A duplicate/redelivered task for the same message (the claim was never released
    # once retries were exhausted, so this must be rejected before the lock is touched
    # again at all).
    tasks.process_inbound.apply_async(kwargs=kwargs)

    row.refresh_from_db()
    assert len(calls) == calls_after_first_delivery
    assert row.processing_completed_at is None
    assert len(fake_meta.requests) == 0

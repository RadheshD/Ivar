"""``IVA_INLINE_PROCESSING`` (development only): the webhook runs the same processing
core synchronously, with no Celery worker or broker involved. Claim/lock/rate-limit/
at-most-once behaviour must be identical to the worker path; only the lock-retry
mechanism differs (a blocking sleep instead of Celery's ``self.retry()``).

``customer_lock`` is replaced with a fake that times out a controlled number of times,
exactly as in ``test_lock_retry.py`` (which these tests mirror for the inline path), so
these tests are deterministic and need no real Redis contention. ``time.sleep`` is
patched to a no-op so the retry countdown doesn't actually slow the test down.
"""

from __future__ import annotations

import logging

import pytest
from django.test import Client, override_settings

from apps.conversations.engine import reset_engine
from apps.conversations.models import ConversationMessage
from apps.whatsapp import tasks
from apps.whatsapp.client import WhatsAppClient

from .fakes import (
    PHONE_NUMBER_ID,
    SETTINGS,
    TOKEN,
    FakeTransport,
    encode,
    payload,
    sign,
    text_message,
)
from .test_lock_retry import _flaky_lock

pytestmark = pytest.mark.django_db


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
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    reset_engine()
    with override_settings(**SETTINGS, IVA_INLINE_PROCESSING=True):
        yield transport
    reset_engine()


def test_inline_mode_never_enqueues_a_celery_task(fake_meta, monkeypatch):
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("inline mode must not enqueue a Celery task")

    monkeypatch.setattr("apps.whatsapp.views.enqueue", _fail_if_called)

    response = signed_post(payload([text_message("hi", "wamid.INLINE1")]))

    assert response.status_code == 200
    row = ConversationMessage.objects.get(wa_message_id="wamid.INLINE1")
    assert row.processing_completed_at is not None
    assert len(fake_meta.requests) == 1  # the reply was actually sent, synchronously


def test_inline_retry_succeeds_after_the_lock_frees(fake_meta, monkeypatch):
    fake_lock, calls = _flaky_lock(fail_times=2)
    monkeypatch.setattr(tasks, "customer_lock", fake_lock)

    response = signed_post(payload([text_message("hi", "wamid.INLINE_RETRY_OK")]))

    assert response.status_code == 200
    assert len(calls) == 3  # 2 timeouts, then the retry that finally acquires it
    row = ConversationMessage.objects.get(wa_message_id="wamid.INLINE_RETRY_OK")
    assert row.processing_completed_at is not None
    assert row.stall_reported_at is None
    assert len(fake_meta.requests) == 1  # the engine ran exactly once, on the successful attempt


def test_inline_retries_exhausted_reports_stalled_once(fake_meta, monkeypatch, caplog):
    fake_lock, calls = _flaky_lock(fail_times=999)  # never frees
    monkeypatch.setattr(tasks, "customer_lock", fake_lock)

    with caplog.at_level(logging.WARNING):
        response = signed_post(payload([text_message("hi", "wamid.INLINE_EXHAUSTED")]))

    assert response.status_code == 200
    assert len(calls) == tasks.MAX_LOCK_RETRIES + 1  # the initial attempt plus every retry
    row = ConversationMessage.objects.get(wa_message_id="wamid.INLINE_EXHAUSTED")
    assert row.processing_completed_at is None
    assert row.stall_reported_at is not None
    assert len(fake_meta.requests) == 0  # the engine never ran; no reply was ever sent
    exhausted_warnings = [r for r in caplog.records if "lock retries exhausted" in r.getMessage()]
    assert len(exhausted_warnings) == 1


def test_webhook_returns_200_even_when_inline_processing_raises(fake_meta, monkeypatch, caplog):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(tasks, "client_from_settings", _boom)

    with caplog.at_level(logging.ERROR):
        response = signed_post(payload([text_message("hi", "wamid.INLINE_CRASH")]))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    row = ConversationMessage.objects.get(wa_message_id="wamid.INLINE_CRASH")
    # Claimed but never completed: the same state a worker crash would leave, so
    # report_stalled_messages will surface it after STALL_AFTER.
    assert row.processing_started_at is not None
    assert row.processing_completed_at is None
    error_logs = [r for r in caplog.records if "inline processing failed" in r.getMessage()]
    assert len(error_logs) == 1
    assert "9198" not in error_logs[0].getMessage()  # the phone number is masked, not logged raw

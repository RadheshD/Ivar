"""Claimed-but-unfinished processing is logged (masked). The warning itself needs no DB;
finding stalled rows and the task's completion marker do (marked django_db, pending)."""

import logging
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.conversations.models import ConversationMessage
from apps.whatsapp import services
from apps.whatsapp.parser import parse_webhook

from .fakes import WA_ID, payload, text_message

NOW = timezone.now()


def fake_row(pk, minutes_ago):
    return SimpleNamespace(
        pk=pk,
        processing_started_at=NOW - timedelta(minutes=minutes_ago),
        conversation=SimpleNamespace(wa_id_masked="91******0001"),
    )


def test_warning_is_logged_and_masked(caplog):
    with caplog.at_level(logging.WARNING, logger="apps.whatsapp.services"):
        count = services.warn_stalled([fake_row(7, 6), fake_row(8, 42)], NOW)
    assert count == 2
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "processing stalled: message=7 to=91******0001 claimed 6 min ago; not retried",
        "processing stalled: message=8 to=91******0001 claimed 42 min ago; not retried",
    ]
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert all(WA_ID not in m for m in messages)


def test_threshold_is_five_minutes():
    assert services.STALL_AFTER == timedelta(minutes=5)


# --------------------------------------------------------------------------- needs PostgreSQL


def claimed_row(minutes_ago, message_id):
    [message] = parse_webhook(payload([text_message("hi", message_id)])).messages
    row, _ = services.record_inbound(message)
    services.claim_for_processing(row.pk)
    ConversationMessage.objects.filter(pk=row.pk).update(
        processing_started_at=timezone.now() - timedelta(minutes=minutes_ago)
    )
    return row


@pytest.mark.django_db
def test_stalled_messages_are_reported_once(caplog):
    stalled = claimed_row(6, "wamid.STALL")
    recent = claimed_row(2, "wamid.RECENT")
    done = claimed_row(30, "wamid.DONE")
    services.complete_processing(done.pk)
    with caplog.at_level(logging.WARNING, logger="apps.whatsapp.services"):
        assert services.report_stalled_messages() == 1
        assert services.report_stalled_messages() == 0  # already reported
    assert f"message={stalled.pk} " in caplog.records[0].getMessage()
    assert all(f"message={recent.pk} " not in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_report_command():
    claimed_row(10, "wamid.CMD")
    out = StringIO()
    call_command("report_stalled_messages", stdout=out)
    assert "Reported 1 stalled message(s)." in out.getvalue()

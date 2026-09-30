"""Models, deduplication, claims, statuses, retention and the end-to-end webhook.

These need PostgreSQL (marked ``django_db``). Until DATABASE_URL points at the
organization's development database they are PENDING, and S2 is not complete.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.conversations.engine import reset_engine
from apps.conversations.models import Conversation, ConversationMessage
from apps.conversations.retention import purge_old_messages
from apps.whatsapp import services, tasks
from apps.whatsapp.client import WhatsAppClient
from apps.whatsapp.parser import StatusUpdate, parse_webhook

from .fakes import (
    PHONE_NUMBER_ID,
    SETTINGS,
    TOKEN,
    WA_ID,
    FakeTransport,
    encode,
    payload,
    sign,
    status,
    text_message,
)

pytestmark = pytest.mark.django_db


def inbound(body="hi", message_id="wamid.IN1"):
    [message] = parse_webhook(payload([text_message(body, message_id)])).messages
    return message


def test_record_inbound_deduplicates_by_whatsapp_message_id():
    row, created = services.record_inbound(inbound())
    again, created_again = services.record_inbound(inbound())
    assert created and not created_again
    assert again.pk == row.pk
    assert ConversationMessage.objects.count() == 1


def test_message_text_and_raw_number_are_never_stored():
    services.record_inbound(inbound("my address is Flat 12 Green Park Road"))
    conversation = Conversation.objects.get()
    assert conversation.wa_id_hash != WA_ID and len(conversation.wa_id_hash) == 64
    assert conversation.wa_id_masked == "91******0001"
    row = ConversationMessage.objects.get()
    assert row.text_length == len("my address is Flat 12 Green Park Road")
    stored = " ".join(str(v) for v in row.__dict__.values()) + str(conversation.__dict__)
    assert "Flat 12" not in stored and WA_ID not in stored


def test_claim_is_granted_once():
    row, _ = services.record_inbound(inbound())
    assert services.claim_for_processing(row.pk) is True
    assert services.claim_for_processing(row.pk) is False


def test_forget_inbound_only_before_processing():
    row, _ = services.record_inbound(inbound())
    services.forget_inbound(row.pk)
    assert not ConversationMessage.objects.exists()
    row, _ = services.record_inbound(inbound())
    services.claim_for_processing(row.pk)
    services.forget_inbound(row.pk)
    assert ConversationMessage.objects.filter(pk=row.pk).exists()


def test_status_moves_forward_only():
    conversation = services.conversation_for(WA_ID)
    services.record_outbound(
        conversation, message_type="text", template_key="help", wa_message_id="wamid.OUT1"
    )
    now = timezone.now()
    assert services.record_status(StatusUpdate("wamid.OUT1", "read", now))
    assert not services.record_status(StatusUpdate("wamid.OUT1", "delivered", now))
    assert ConversationMessage.objects.get(wa_message_id="wamid.OUT1").status == "read"
    assert services.record_status(StatusUpdate("wamid.OUT1", "failed", now, 131026))
    row = ConversationMessage.objects.get(wa_message_id="wamid.OUT1")
    assert (row.status, row.error_code) == ("failed", 131026)
    assert not services.record_status(StatusUpdate("wamid.UNKNOWN", "read", now))


def test_failed_send_is_recorded_without_a_message_id():
    conversation = services.conversation_for(WA_ID)
    row = services.record_outbound(
        conversation, message_type="text", template_key="help", wa_message_id=None, error_code=4
    )
    assert (row.status, row.error_code, row.wa_message_id) == ("failed", 4, None)


def test_24_hour_window():
    services.record_inbound(inbound())
    conversation = Conversation.objects.get()
    assert services.can_reply(conversation)
    assert not services.can_reply(conversation, now=timezone.now() + timedelta(hours=25))


def test_retention_purge_and_command():
    row, _ = services.record_inbound(inbound())
    ConversationMessage.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(days=91)
    )
    services.record_inbound(inbound(message_id="wamid.NEW"))
    assert purge_old_messages() == 1
    out = StringIO()
    call_command("purge_conversation_messages", "--days", "1", stdout=out)
    assert "Deleted 0" in out.getvalue()
    assert ConversationMessage.objects.count() == 1


# --------------------------------------------------------------------------- end to end


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


def signed_post(data):
    body = encode(data)
    return Client().post(
        "/webhooks/whatsapp/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=sign(body),
    )


def test_webhook_end_to_end_with_eager_task(fake_meta):
    response = signed_post(payload([text_message("hi", "wamid.E2E")]))
    assert response.status_code == 200
    assert len(fake_meta.requests) == 1  # the language prompt (greets the webhook's profile name)
    inbound_row = ConversationMessage.objects.get(wa_message_id="wamid.E2E")
    assert inbound_row.processing_started_at is not None
    assert inbound_row.processing_completed_at is not None  # finished: never reported stalled
    outbound = ConversationMessage.objects.get(direction="out")
    assert (outbound.template_key, outbound.status, outbound.wa_message_id) == (
        "language.prompt_greeting_named",
        "sent",
        "wamid.OUT1",
    )


def test_webhook_duplicate_is_processed_once(fake_meta):
    data = payload([text_message("hi", "wamid.DUP")])
    signed_post(data)
    signed_post(data)
    assert len(fake_meta.requests) == 1
    assert ConversationMessage.objects.filter(direction="in").count() == 1


def test_status_callback_end_to_end(fake_meta):
    signed_post(payload([text_message("hi", "wamid.S1")]))
    signed_post(payload(statuses=[status("wamid.OUT1", "delivered")]))
    assert ConversationMessage.objects.get(wa_message_id="wamid.OUT1").status == "delivered"
    assert len(fake_meta.requests) == 1  # never replied to

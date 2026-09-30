"""Persistent conversation records: metadata for deduplication and auditing only.

Stored: a keyed pseudonym and a masked form of the WhatsApp ID, message IDs, types,
statuses, timestamps, text length, reply IDs (engine-generated, no customer text) and
the template key of each reply. NOT stored: message text, customer names, addresses or
the raw phone number. Rows older than ``IVA_MESSAGE_RETENTION_DAYS`` (default 90) are
deleted by ``manage.py purge_conversation_messages``.
"""

from django.db import models


class Conversation(models.Model):
    wa_id_hash = models.CharField(max_length=64, unique=True)
    wa_id_masked = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Conversation {self.pk} ({self.wa_id_masked})"


class ConversationMessage(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "in", "Inbound"
        OUTBOUND = "out", "Outbound"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=3, choices=Direction.choices)
    # WhatsApp's message ID. The unique constraint is what deduplicates webhook
    # deliveries. Outbound rows get it from the send response (null if sending failed).
    wa_message_id = models.CharField(max_length=200, unique=True, null=True, blank=True)
    message_type = models.CharField(max_length=32)
    reply_id = models.CharField(max_length=200, blank=True, default="")
    template_key = models.CharField(max_length=64, blank=True, default="")
    text_length = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_code = models.IntegerField(null=True, blank=True)
    wa_timestamp = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status_at = models.DateTimeField(null=True, blank=True)
    # Set once when a worker claims an inbound message (at-most-once processing).
    processing_started_at = models.DateTimeField(null=True, blank=True)
    # Set when that worker finished. Claimed but not finished after a few minutes means
    # processing crashed; it is logged once (stall_reported_at) and never retried.
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    stall_reported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"{self.direction} {self.message_type} {self.status}"

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
    # The customer's choice about pushed order-status notifications (typed "stop order
    # updates" / "resume order updates"). On by default; a plain flag, no customer text.
    order_updates_enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"Conversation {self.pk} ({self.wa_id_masked})"


class OrderEventRecord(models.Model):
    """One pushed order-status event, stored once (the unique ``event_id`` is the idempotency
    guard: a redelivery can never notify twice). Holds the backend's own IDs only: no phone
    number, name or address. Not subject to the message-retention purge, which would
    re-open the door to a duplicate."""

    class Outcome(models.TextChoices):
        RECEIVED = "received", "Received, not yet handled"
        SENT_FREE_FORM = "sent_free_form", "Sent as a free-form message"
        SENT_TEMPLATE = "sent_template", "Sent as a template message"
        UNDELIVERED_NO_TEMPLATE = "undelivered_no_template", "Outside window, no template"
        OPTED_OUT = "opted_out", "Customer stopped order updates"
        SUPERSEDED = "superseded", "Order no longer in that status"
        UNKNOWN_CUSTOMER = "unknown_customer", "Customer not found"
        UNKNOWN_ORDER = "unknown_order", "Order not found for the customer"
        SEND_FAILED = "send_failed", "WhatsApp send failed"
        BACKEND_UNAVAILABLE = "backend_unavailable", "Backend unavailable"

    event_id = models.CharField(max_length=128, unique=True)
    order_number = models.CharField(max_length=64)
    new_status = models.CharField(max_length=32)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, default=Outcome.RECEIVED)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"OrderEvent {self.pk} {self.new_status} {self.outcome}"


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

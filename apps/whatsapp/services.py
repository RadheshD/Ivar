"""Database side of the channel: dedup, processing claims and delivery statuses.

Only metadata is stored (see ``apps.conversations.models``); message text never is.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations.models import Conversation, ConversationMessage
from apps.iva_common.logging import mask_phone
from apps.iva_common.pseudonym import pseudonymize

from .parser import InboundMessage, StatusUpdate

logger = logging.getLogger(__name__)

CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)
# A claimed message not completed within this time is treated as crashed processing.
STALL_AFTER = timedelta(minutes=5)
Status = ConversationMessage.Status
Direction = ConversationMessage.Direction
# A status only moves forward; "failed" can arrive at any point.
_STATUS_RANK = {Status.SENT: 1, Status.DELIVERED: 2, Status.READ: 3}


def conversation_for(wa_id: str) -> Conversation:
    conversation, _ = Conversation.objects.get_or_create(
        wa_id_hash=pseudonymize(wa_id), defaults={"wa_id_masked": mask_phone(wa_id)}
    )
    return conversation


def record_inbound(message: InboundMessage) -> tuple[ConversationMessage, bool]:
    """Store an inbound message once. Returns ``(row, created)``; a duplicate is not created."""
    conversation = conversation_for(message.wa_id)
    try:
        with transaction.atomic():
            row = ConversationMessage.objects.create(
                conversation=conversation,
                direction=Direction.INBOUND,
                wa_message_id=message.wa_message_id,
                message_type=message.message_type,
                reply_id=message.reply_id or "",
                text_length=len(message.text or ""),
                status=Status.RECEIVED,
                wa_timestamp=message.timestamp,
            )
    except IntegrityError:
        return ConversationMessage.objects.get(wa_message_id=message.wa_message_id), False
    Conversation.objects.filter(pk=conversation.pk).update(last_inbound_at=timezone.now())
    return row, True


def forget_inbound(message_pk: int) -> None:
    """Undo ``record_inbound`` when the message could not be queued, so Meta's retry is kept."""
    ConversationMessage.objects.filter(pk=message_pk, processing_started_at__isnull=True).delete()


def claim_for_processing(message_pk: int) -> bool:
    """Atomically mark an inbound message as taken. False if already taken (at most once)."""
    updated = ConversationMessage.objects.filter(
        pk=message_pk, direction=Direction.INBOUND, processing_started_at__isnull=True
    ).update(processing_started_at=timezone.now())
    return updated == 1


def complete_processing(message_pk: int) -> None:
    ConversationMessage.objects.filter(pk=message_pk).update(processing_completed_at=timezone.now())


def release_claim(message_pk: int) -> None:
    """Undo ``claim_for_processing`` so a message safe to retry (nothing has happened
    yet) can be reclaimed by the retried task. Guarded against a message that somehow
    already completed, so a retry can never un-claim finished work."""
    ConversationMessage.objects.filter(pk=message_pk, processing_completed_at__isnull=True).update(
        processing_started_at=None
    )


def report_lock_exhausted(message_pk: int, wa_id: str) -> None:
    """Mark a message stalled/reported immediately once its per-customer lock retries
    are exhausted: the same terminal state ``report_stalled_messages`` would reach after
    ``STALL_AFTER``, just known right away instead of waiting for that sweep. The claim
    is left in place (not released), so the message can never be reclaimed and
    processed again. Guarded so a duplicate call never logs or reports twice."""
    updated = ConversationMessage.objects.filter(
        pk=message_pk, stall_reported_at__isnull=True
    ).update(stall_reported_at=timezone.now())
    if updated:
        logger.warning(
            "processing stalled: message=%s to=%s per-customer lock retries exhausted; "
            "not retried further",
            message_pk, mask_phone(wa_id),
        )  # fmt: skip


def stalled_messages(now=None):
    """Inbound messages claimed more than ``STALL_AFTER`` ago, unfinished, not yet reported."""
    now = now or timezone.now()
    return ConversationMessage.objects.select_related("conversation").filter(
        direction=Direction.INBOUND,
        processing_started_at__lt=now - STALL_AFTER,
        processing_completed_at__isnull=True,
        stall_reported_at__isnull=True,
    )


def warn_stalled(rows, now=None) -> int:
    """Log one masked warning per stalled message. No customer text exists to log."""
    now = now or timezone.now()
    count = 0
    for row in rows:
        minutes = int((now - row.processing_started_at).total_seconds() // 60)
        logger.warning(
            "processing stalled: message=%s to=%s claimed %d min ago; not retried",
            row.pk, row.conversation.wa_id_masked, minutes,
        )  # fmt: skip
        count += 1
    return count


def report_stalled_messages(now=None) -> int:
    """Warn about each stalled message once, then mark it reported."""
    now = now or timezone.now()
    rows = list(stalled_messages(now))
    count = warn_stalled(rows, now)
    if rows:
        ConversationMessage.objects.filter(pk__in=[r.pk for r in rows]).update(
            stall_reported_at=now
        )
    return count


def can_reply(conversation: Conversation, now=None) -> bool:
    """Replies only inside WhatsApp's 24-hour customer-service window after an inbound."""
    now = now or timezone.now()
    last = conversation.last_inbound_at
    return last is not None and now - last <= CUSTOMER_SERVICE_WINDOW


def record_outbound(
    conversation: Conversation,
    *,
    message_type: str,
    template_key: str,
    wa_message_id: str | None,
    error_code: int | None = None,
) -> ConversationMessage:
    now = timezone.now()
    row = ConversationMessage.objects.create(
        conversation=conversation,
        direction=Direction.OUTBOUND,
        wa_message_id=wa_message_id,
        message_type=message_type,
        template_key=template_key[:64],
        status=Status.SENT if wa_message_id else Status.FAILED,
        error_code=error_code,
        status_at=now,
    )
    if wa_message_id:
        Conversation.objects.filter(pk=conversation.pk).update(last_outbound_at=now)
    return row


def record_status(update: StatusUpdate) -> bool:
    """Apply a delivery status callback to our outbound message. Never replied to."""
    row = ConversationMessage.objects.filter(
        wa_message_id=update.wa_message_id, direction=Direction.OUTBOUND
    ).first()
    if row is None:
        return False
    new = Status(update.status)
    if new != Status.FAILED and _STATUS_RANK.get(new, 0) <= _STATUS_RANK.get(row.status, 0):
        return False  # out-of-order or repeated callback
    row.status = new
    row.status_at = update.timestamp or timezone.now()
    if new == Status.FAILED:
        row.error_code = update.error_code
    row.save(update_fields=["status", "status_at", "error_code"])
    return True

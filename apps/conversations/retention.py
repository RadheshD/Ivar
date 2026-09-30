"""Retention for conversation message metadata (message text is never stored)."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import ConversationMessage

DEFAULT_RETENTION_DAYS = 90


def retention_days() -> int:
    try:
        days = int(getattr(settings, "IVA_MESSAGE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return days if days > 0 else DEFAULT_RETENTION_DAYS


def purge_old_messages(days: int | None = None) -> int:
    """Delete message rows older than the retention period. Returns rows deleted."""
    cutoff = timezone.now() - timedelta(days=days or retention_days())
    deleted, _ = ConversationMessage.objects.filter(created_at__lt=cutoff).delete()
    return deleted

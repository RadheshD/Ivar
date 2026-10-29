"""A customer's choice to receive (or stop) pushed order-status updates.

Stored in Postgres on the customer's ``Conversation`` row (keyed by the WhatsApp ID's
pseudonym), never in Redis. No row means the customer never opted out: updates are on.
"""

from __future__ import annotations

from apps.iva_common.logging import mask_phone
from apps.iva_common.pseudonym import pseudonymize

from .models import Conversation


def updates_enabled(whatsapp_id: str) -> bool:
    flag = (
        Conversation.objects.filter(wa_id_hash=pseudonymize(whatsapp_id))
        .values_list("order_updates_enabled", flat=True)
        .first()
    )
    return True if flag is None else bool(flag)


def set_updates_enabled(whatsapp_id: str, enabled: bool) -> None:
    conversation, _ = Conversation.objects.get_or_create(
        wa_id_hash=pseudonymize(whatsapp_id), defaults={"wa_id_masked": mask_phone(whatsapp_id)}
    )
    if conversation.order_updates_enabled != enabled:
        Conversation.objects.filter(pk=conversation.pk).update(order_updates_enabled=enabled)

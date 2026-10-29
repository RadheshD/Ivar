"""Order-status notifications: the backend PUSHES an ``OrderStatusChangeEvent``; this module
decides whether and how to tell the customer. IVA never polls order status.

Pipeline for one event (``handle_event``):

1. ``claim`` stores the event in Postgres once. The unique ``event_id`` is the idempotency
   guard, so a redelivered event can never notify twice. Nothing here touches Redis.
2. The customer and order are re-read through the Backend Port. An event whose order is no
   longer in that status (a later change superseded it) is not notified.
3. A customer who typed "stop order updates" is skipped.
4. Inside the 24-hour customer-service window: a free-form message in the customer's
   language. Outside it: a pre-approved template from ``settings.IVA_ORDER_STATUS_TEMPLATES``
   for that status AND language. No template configured means NOT sent: the event is
   recorded as undelivered and logged. A template name is never guessed.

Only facts from the event or the order are stated (the status and the order number); no ETA,
tracking details or times are ever invented. Logs carry event IDs, statuses and outcome
names only: never a name, address, phone number or WhatsApp ID.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations import order_updates
from apps.conversations.backend_port import (
    LANGUAGES,
    BackendPort,
    NotAuthorized,
    OrderStatusChangeEvent,
)
from apps.conversations.channel import OutTemplate, OutText
from apps.conversations.messages import t
from apps.conversations.models import Conversation, OrderEventRecord
from apps.iva_common.pseudonym import pseudonymize

from . import services
from .client import WhatsAppClient
from .delivery import send_replies

logger = logging.getLogger(__name__)

Outcome = OrderEventRecord.Outcome


def claim(event: OrderStatusChangeEvent) -> bool:
    """Store the event; ``False`` if this ``event_id`` was already received (a duplicate)."""
    try:
        with transaction.atomic():  # a duplicate must not poison a surrounding transaction
            OrderEventRecord.objects.create(
                event_id=event.event_id,
                order_number=event.order_number,
                new_status=event.new_status,
            )
    except IntegrityError:
        return False
    return True


def release(event_id: str) -> None:
    """Forget a claimed event that could not be queued, so the backend's retry is accepted."""
    OrderEventRecord.objects.filter(event_id=event_id, outcome=Outcome.RECEIVED).delete()


def finish(event_id: str, outcome: str) -> str:
    OrderEventRecord.objects.filter(event_id=event_id).update(
        outcome=outcome, finished_at=timezone.now()
    )
    return outcome


def template_name(status: str, language: str) -> str | None:
    """The configured template for this status and language, or ``None``. No fallback to
    another language or status: that would be a guess."""
    by_language = (getattr(settings, "IVA_ORDER_STATUS_TEMPLATES", None) or {}).get(status) or {}
    return by_language.get(language) or None


def handle_event(
    event: OrderStatusChangeEvent, *, backend: BackendPort, client: WhatsAppClient
) -> str:
    """Handle one claimed event and record its final outcome (returned).

    A ``BackendUnavailable`` from the port propagates BEFORE anything is sent, so the
    caller may retry safely; every other path ends with a recorded outcome."""
    customer = backend.get_customer(event.customer_id)
    if customer is None:
        return _done(event, Outcome.UNKNOWN_CUSTOMER)
    try:
        view = backend.get_order(customer.id, event.order_number)
    except NotAuthorized:
        view = None  # another customer's order: never revealed, never notified
    if view is None:
        return _done(event, Outcome.UNKNOWN_ORDER)
    if view.order_status != event.new_status:
        return _done(event, Outcome.SUPERSEDED)
    wa_id = customer.whatsapp_id
    if not order_updates.updates_enabled(wa_id):
        return _done(event, Outcome.OPTED_OUT)

    language = customer.language if customer.language in LANGUAGES else "en"
    conversation = Conversation.objects.filter(wa_id_hash=pseudonymize(wa_id)).first()
    if conversation is not None and services.can_reply(conversation):
        key = f"notify.order_status.{event.new_status}"
        body = t(language, key, order=event.order_number) + "\n\n" + t(language, "notify.footer")
        message = OutText(body, key)
        success = Outcome.SENT_FREE_FORM
    else:
        name = template_name(event.new_status, language)
        if name is None:
            logger.warning(
                "order event not sent: event=%s status=%s reason=no_template_outside_window",
                event.event_id, event.new_status,
            )  # fmt: skip
            return _done(event, Outcome.UNDELIVERED_NO_TEMPLATE)
        message = OutTemplate(
            name, language, (event.order_number,), key=f"notify.template.{event.new_status}"
        )
        success = Outcome.SENT_TEMPLATE

    results = send_replies(wa_id, [message], client=client)
    conversation = conversation or services.conversation_for(wa_id)
    for sent in results:
        services.record_outbound(
            conversation,
            message_type=sent.message_type,
            template_key=sent.template_key,
            wa_message_id=sent.wa_message_id,
            error_code=sent.error.code if sent.error else None,
        )
    return _done(event, success if results and results[0].ok else Outcome.SEND_FAILED)


def _done(event: OrderStatusChangeEvent, outcome: str) -> str:
    finish(event.event_id, outcome)
    logger.info(
        "order event handled: event=%s status=%s outcome=%s",
        event.event_id, event.new_status, outcome,
    )  # fmt: skip
    return outcome

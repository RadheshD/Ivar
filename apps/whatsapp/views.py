"""WhatsApp webhook: GET for Meta's subscription check, POST for events.

POST verifies ``X-Hub-Signature-256`` over the raw body before anything else; an
invalid signature gets 403 and nothing is processed. Valid events are recorded (the
WhatsApp message ID's unique constraint drops duplicates) and queued (or, with
``IVA_INLINE_PROCESSING``, processed synchronously — development only); the response is
200 straight away. If recording or queueing fails, 503 asks Meta to retry later; once a
message is recorded, the response is always 200 even if inline processing itself then
fails (the message stays claimed, surfaced by report_stalled_messages, same as a worker
crash — Meta must not redeliver something we already have).
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.conversations.backend_port import InvalidOrderEvent, parse_order_status_change
from apps.iva_common.logging import mask_phone

from . import order_events, ordering, services
from .parser import parse_webhook
from .security import SIGNATURE_HEADER, verify_signature, verify_token
from .tasks import (
    ORDER_EVENT_KWARGSREPR,
    enqueue,
    notify_order_status,
    notify_order_status_inline,
    order_event_fields,
    process_inbound_inline,
)

logger = logging.getLogger(__name__)

MAX_CHALLENGE_LENGTH = 256
EVENTS_SECRET_HEADER = "X-IVA-Events-Secret"  # noqa: S105  (a header name, not a secret)
MAX_EVENT_BODY_BYTES = 4096


@csrf_exempt  # Meta authenticates with the HMAC signature, not a CSRF token.
@require_http_methods(["GET", "POST"])
def webhook(request):
    if request.method == "GET":
        return _verify_subscription(request)
    return _receive(request)


def _verify_subscription(request):
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token")
    challenge = request.GET.get("hub.challenge", "")
    expected = getattr(settings, "WHATSAPP_VERIFY_TOKEN", "")
    valid_challenge = (
        0 < len(challenge) <= MAX_CHALLENGE_LENGTH and challenge.isascii() and challenge.isalnum()
    )
    if mode == "subscribe" and valid_challenge and verify_token(expected, token):
        return HttpResponse(challenge, content_type="text/plain")
    logger.warning("webhook: subscription verification refused")
    return HttpResponse(status=403)


def _receive(request):
    secret = getattr(settings, "WHATSAPP_APP_SECRET", "")
    if not secret:
        logger.error("webhook: WHATSAPP_APP_SECRET is not configured")
        return HttpResponse(status=403)
    if not verify_signature(secret, request.body, request.headers.get(SIGNATURE_HEADER)):
        logger.warning("webhook: invalid signature")
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        logger.warning("webhook: body is not JSON")
        return JsonResponse({"status": "invalid"}, status=400)

    parsed = parse_webhook(
        payload, phone_number_id=getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or None
    )
    inline = getattr(settings, "IVA_INLINE_PROCESSING", False)
    try:
        for message in parsed.messages:
            row, created = services.record_inbound(message)
            if not created:
                logger.info("webhook: duplicate message ignored")
                continue
            if inline:
                _process_inline(row.pk, message)
            else:
                try:
                    enqueue(row.pk, message)
                except Exception:
                    services.forget_inbound(row.pk)  # let Meta's retry deliver it again
                    raise
        for status in parsed.statuses:
            ordering.note_status(status.wa_message_id, status.status)  # frees a waiting worker
            services.record_status(status)
    except Exception as exc:
        logger.error("webhook: could not record or queue events error=%s", type(exc).__name__)
        return JsonResponse({"status": "retry"}, status=503)
    return JsonResponse({"status": "ok"})


def _process_inline(message_pk, message) -> None:
    """The message is already recorded and claimed by the time this runs, so a failure
    here must never turn into a 503 — Meta must not redeliver something we already
    have. Left claimed-but-incomplete, it surfaces via report_stalled_messages exactly
    as a worker crash would."""
    try:
        process_inbound_inline(
            message_pk,
            message.wa_id,
            message.text,
            message.reply_id,
            message.reply_title,
            message.profile_name,
            message.order,
        )
    except Exception as exc:
        logger.error(
            "webhook: inline processing failed to=%s error=%s",
            mask_phone(message.wa_id), type(exc).__name__,
        )  # fmt: skip


def _event_reply(status: str, http_status: int = 200) -> JsonResponse:
    """The only bodies the order-events endpoint ever sends: a bare status word."""
    return JsonResponse({"status": status}, status=http_status)


def _declared_length_ok(request) -> bool:
    """True only for a ``Content-Length`` that is a plain non-negative integer within the cap.
    A request with no length (for example chunked) is refused: the backend must send one."""
    declared = request.META.get("CONTENT_LENGTH")
    if not isinstance(declared, str) or not declared.isascii() or not declared.isdigit():
        return False
    return int(declared) <= MAX_EVENT_BODY_BYTES


@csrf_exempt  # The backend authenticates with a shared secret header, not a CSRF token.
@require_http_methods(["POST"])
def order_status_events(request):
    """The backend pushes an ``OrderStatusChangeEvent`` here (see ARCHITECTURE.md).

    Unauthenticated, oversized, undecodable or malformed requests are all refused with a bare
    status word: nothing about why, and nothing from a refused request, is ever echoed or
    logged. The declared ``Content-Length`` is checked (after authentication) BEFORE the body is
    read: missing, invalid or over ``MAX_EVENT_BODY_BYTES`` is refused unread; the length check
    on the bytes actually read stays as a backstop. A repeated ``event_id`` is acknowledged
    (200 "duplicate") without notifying again."""
    given = request.headers.get(EVENTS_SECRET_HEADER)
    if not verify_token(getattr(settings, "IVA_ORDER_EVENTS_SECRET", ""), given):
        logger.warning("order event refused: reason=auth")
        return _event_reply("rejected", 403)
    if not _declared_length_ok(request):
        logger.warning("order event refused: reason=length")
        return _event_reply("rejected", 400)
    raw = request.body
    try:
        if len(raw) > MAX_EVENT_BODY_BYTES:
            raise InvalidOrderEvent("Too large.")
        event = parse_order_status_change(json.loads(raw))
    except (InvalidOrderEvent, ValueError, RecursionError):  # bad JSON/UTF-8 are ValueErrors
        logger.warning("order event refused: reason=malformed")
        return _event_reply("rejected", 400)
    if not order_events.claim(event):
        logger.info("order event duplicate: event=%s", event.event_id)
        return _event_reply("duplicate")
    fields = order_event_fields(event)
    if getattr(settings, "IVA_INLINE_PROCESSING", False):
        try:
            notify_order_status_inline(fields)
        except Exception as exc:  # claimed already: the backend must not redeliver
            logger.error(
                "order event inline processing failed: event=%s error=%s",
                event.event_id, type(exc).__name__,
            )  # fmt: skip
        return _event_reply("accepted")
    try:
        # kwargsrepr keeps the event out of Celery's logs (the same way ``tasks.enqueue`` masks
        # the inbound task); the broker payload still carries the fields the task needs.
        notify_order_status.apply_async(
            kwargs={"fields": fields}, kwargsrepr=ORDER_EVENT_KWARGSREPR
        )
    except Exception as exc:
        order_events.release(event.event_id)  # not queued: let the backend's retry in
        logger.error(
            "order event could not be queued: event=%s error=%s",
            event.event_id, type(exc).__name__,
        )  # fmt: skip
        return _event_reply("unavailable", 503)
    return _event_reply("accepted")

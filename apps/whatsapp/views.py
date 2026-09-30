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

from apps.iva_common.logging import mask_phone

from . import services
from .parser import parse_webhook
from .security import SIGNATURE_HEADER, verify_signature, verify_token
from .tasks import enqueue, process_inbound_inline

logger = logging.getLogger(__name__)

MAX_CHALLENGE_LENGTH = 256


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
        )
    except Exception as exc:
        logger.error(
            "webhook: inline processing failed to=%s error=%s",
            mask_phone(message.wa_id), type(exc).__name__,
        )  # fmt: skip

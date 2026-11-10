"""Run the engine for one inbound message and send its replies. No database access."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from apps.conversations.channel import (
    WELCOME_BANNER_REF,
    Inbound,
    Outbound,
    OutButtons,
    OutCarousel,
    OutProductCard,
    OutText,
)
from apps.conversations.renderers import render
from apps.iva_common.logging import mask_phone

from . import banner
from .client import WhatsAppClient, WhatsAppSendError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentReply:
    template_key: str
    message_type: str  # "text", "button", "list" or "carousel"
    wa_message_id: str | None
    error: WhatsAppSendError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _message_type(payload: dict) -> str:
    if payload.get("type") == "interactive":
        return payload["interactive"]["type"]
    return payload.get("type", "unknown")


def _is_large(reply: Outbound) -> bool:
    """A message whose images WhatsApp has to fetch before it can be delivered."""
    return isinstance(reply, OutCarousel) or (
        isinstance(reply, OutProductCard) and bool(reply.image_url or reply.image_media_id)
    )


def _media_rejected(exc: WhatsAppSendError) -> bool:
    """A client-side HTTP error (not a rate limit): an expired or invalid media ID looks like
    this, and only a fresh upload can fix it. Timeouts, 5xx and 429 are not that."""
    return (
        exc.kind == "http"
        and exc.status is not None
        and 400 <= exc.status < 500
        and exc.status != 429
    )


def _next_is_buttons(queue: list[Outbound]) -> bool:
    """Is the next message a plain text or buttons message? Either could overtake a carousel
    or image card that WhatsApp is still preparing (the "nutrition not verified" note is text)."""
    return bool(queue) and isinstance(queue[0], OutButtons | OutText)


def send_replies(
    wa_id: str,
    replies: list[Outbound],
    *,
    client: WhatsAppClient,
    wait_for_status: Callable[[str], bool] | None = None,
) -> list[SentReply]:
    """Replies are sent in order; after a failed send the rest are not sent (order matters).

    A product card whose image fails to send is retried once, immediately, without its
    image header, rather than dropping the product (see ``OutProductCard``). A carousel
    WhatsApp rejects is replaced, in place, by its ordinary cards (``OutCarousel.fallback``).

    Ordering: WhatsApp prepares a carousel or image card (fetching its images) after it has
    accepted the send, so a buttons message sent straight after could arrive first. When
    ``wait_for_status`` is given, a buttons message that follows such a large message is
    held until it returns (it blocks for that message's status callback, up to a timeout)
    and is then sent whatever the result -- a timeout only costs the wait, never the message.
    """
    results: list[SentReply] = []
    queue = list(replies)
    while queue:
        reply = queue.pop(0)
        used_cached_media = False
        if (
            isinstance(reply, OutProductCard)
            and reply.image_ref == WELCOME_BANNER_REF
            and not reply.image_url  # a configured public link keeps the link behaviour
            and not reply.image_media_id
        ):
            # The first-time picker's banner: a cached media ID, or one upload (never raises;
            # no ID means the picker simply goes out without its header).
            media_id, used_cached_media = banner.media_id_for(client)
            if media_id:
                reply = replace(reply, image_media_id=media_id)
        payload = render(reply)
        kind = _message_type(payload)
        try:
            sent = client.send(wa_id, payload)
        except WhatsAppSendError as exc:
            if (
                isinstance(reply, OutProductCard)
                and used_cached_media
                and reply.image_media_id
                and _media_rejected(exc)
            ):
                # The cached ID may have expired: upload again ONCE and retry ONCE. If either
                # step fails, forget the cached ID and fall through to the text-only retry.
                fresh_id, _ = banner.media_id_for(client, force_upload=True)
                if fresh_id:
                    try:
                        sent = client.send(wa_id, render(replace(reply, image_media_id=fresh_id)))
                    except WhatsAppSendError:
                        banner.forget(client)
                    else:
                        logger.warning(
                            "delivery: cached banner media was rejected, re-uploaded: to=%s key=%s",
                            mask_phone(wa_id), reply.key,
                        )  # fmt: skip
                        results.append(SentReply(reply.key or "", kind, sent.wa_message_id))
                        continue
                else:
                    banner.forget(client)
            if isinstance(reply, OutCarousel) and reply.fallback:
                logger.warning(
                    "delivery: carousel send failed, sending cards instead: to=%s key=%s kind=%s",
                    mask_phone(wa_id), reply.key, exc.kind,
                )  # fmt: skip
                queue[:0] = reply.fallback
                continue
            if isinstance(reply, OutProductCard) and (reply.image_url or reply.image_media_id):
                try:
                    sent = client.send(
                        wa_id, render(replace(reply, image_url=None, image_media_id=None))
                    )
                except WhatsAppSendError as retry_exc:
                    results.append(SentReply(reply.key or "", kind, None, retry_exc))
                    logger.error(
                        "delivery stopped: to=%s key=%s kind=%s (image retry also failed)",
                        mask_phone(wa_id), reply.key, retry_exc.kind,
                    )  # fmt: skip
                    break
                logger.warning(
                    "delivery: image send failed, sent card as text-only: to=%s key=%s",
                    mask_phone(wa_id), reply.key,
                )  # fmt: skip
                results.append(SentReply(reply.key or "", kind, sent.wa_message_id))
                continue
            results.append(SentReply(reply.key or "", kind, None, exc))
            logger.error(
                "delivery stopped: to=%s key=%s kind=%s", mask_phone(wa_id), reply.key, exc.kind
            )
            break
        results.append(SentReply(reply.key or "", kind, sent.wa_message_id))
        if wait_for_status and sent.wa_message_id and _is_large(reply) and _next_is_buttons(queue):
            if not wait_for_status(sent.wa_message_id):
                logger.info(
                    "delivery: no status callback in time, sending the follow-up anyway: "
                    "to=%s key=%s", mask_phone(wa_id), reply.key,
                )  # fmt: skip
    return results


def deliver(wa_id: str, inbound: Inbound, *, engine, client: WhatsAppClient) -> list[SentReply]:
    replies: list[Outbound] = engine.handle(wa_id, inbound)
    return send_replies(wa_id, replies, client=client)

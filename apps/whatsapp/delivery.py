"""Run the engine for one inbound message and send its replies. No database access."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from apps.conversations.channel import Inbound, Outbound, OutProductCard
from apps.conversations.renderers import render
from apps.iva_common.logging import mask_phone

from .client import WhatsAppClient, WhatsAppSendError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentReply:
    template_key: str
    message_type: str  # "text", "button" or "list"
    wa_message_id: str | None
    error: WhatsAppSendError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _message_type(payload: dict) -> str:
    if payload.get("type") == "interactive":
        return payload["interactive"]["type"]
    return payload.get("type", "unknown")


def send_replies(wa_id: str, replies: list[Outbound], *, client: WhatsAppClient) -> list[SentReply]:
    """Replies are sent in order; after a failed send the rest are not sent (order matters).

    A product card whose image fails to send is retried once, immediately, without its
    image header, rather than dropping the product (see ``OutProductCard``).
    """
    results: list[SentReply] = []
    for reply in replies:
        payload = render(reply)
        kind = _message_type(payload)
        try:
            sent = client.send(wa_id, payload)
        except WhatsAppSendError as exc:
            if isinstance(reply, OutProductCard) and reply.image_url:
                try:
                    sent = client.send(wa_id, render(replace(reply, image_url=None)))
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
    return results


def deliver(wa_id: str, inbound: Inbound, *, engine, client: WhatsAppClient) -> list[SentReply]:
    replies: list[Outbound] = engine.handle(wa_id, inbound)
    return send_replies(wa_id, replies, client=client)

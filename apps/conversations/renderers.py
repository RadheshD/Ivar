"""Channel-neutral outbound messages -> WhatsApp Cloud API message objects.

Returns the message part only ({"type": ..., ...}); the S2 client adds
``messaging_product`` and ``to``. Text is truncated to WhatsApp's limits with "…".
"""

from __future__ import annotations

from .channel import Outbound, OutButtons, OutCTA, OutList, OutProductCard, OutText

TEXT_BODY_MAX = 4096
INTERACTIVE_BODY_MAX = 1024
BUTTON_TITLE_MAX = 20
LIST_BUTTON_MAX = 20
ROW_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
SECTION_TITLE_MAX = 24
BUTTON_ID_MAX = 256
ROW_ID_MAX = 200


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split()) if limit <= ROW_DESCRIPTION_MAX else str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _checked_id(value: str, limit: int) -> str:
    if not value or len(value) > limit:
        raise ValueError("Reply ID is empty or too long for WhatsApp.")
    return value


def _reply_buttons(buttons) -> list[dict]:
    return [
        {
            "type": "reply",
            "reply": {
                "id": _checked_id(b.id, BUTTON_ID_MAX),
                "title": truncate(b.title, BUTTON_TITLE_MAX),
            },
        }
        for b in buttons
    ]


def render(message: Outbound) -> dict:
    if isinstance(message, OutText):
        return {
            "type": "text",
            "text": {"body": truncate(message.body, TEXT_BODY_MAX), "preview_url": False},
        }
    if isinstance(message, OutButtons):
        return {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": truncate(message.body, INTERACTIVE_BODY_MAX)},
                "action": {"buttons": _reply_buttons(message.buttons)},
            },
        }
    if isinstance(message, OutProductCard):
        interactive = {
            "type": "button",
            "body": {"text": truncate(message.body, INTERACTIVE_BODY_MAX)},
            "action": {"buttons": _reply_buttons(message.buttons)},
        }
        if message.image_url:
            interactive["header"] = {"type": "image", "image": {"link": message.image_url}}
        return {"type": "interactive", "interactive": interactive}
    if isinstance(message, OutList):
        rows = []
        for row in message.rows:
            item = {
                "id": _checked_id(row.id, ROW_ID_MAX),
                "title": truncate(row.title, ROW_TITLE_MAX),
            }
            if row.description:
                item["description"] = truncate(row.description, ROW_DESCRIPTION_MAX)
            rows.append(item)
        section = {"rows": rows}
        if message.section_title:
            section["title"] = truncate(message.section_title, SECTION_TITLE_MAX)
        return {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": truncate(message.body, INTERACTIVE_BODY_MAX)},
                "action": {
                    "button": truncate(message.button, LIST_BUTTON_MAX),
                    "sections": [section],
                },
            },
        }
    if isinstance(message, OutCTA):
        return {
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": truncate(message.body, INTERACTIVE_BODY_MAX)},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": truncate(message.display_text, BUTTON_TITLE_MAX),
                        "url": message.url,
                    },
                },
            },
        }
    raise TypeError("Unknown outbound message type.")


def render_all(messages: list[Outbound]) -> list[dict]:
    return [render(m) for m in messages]

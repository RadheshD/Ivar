"""Parse WhatsApp Cloud API webhook payloads into plain inbound messages and status updates.

Defensive by design: anything malformed or unexpected is counted as ignored, never
raised. The webhook's ``contacts[].profile.name`` is read (matched to a message by
``wa_id``) so the engine can greet a new customer by name; it is never logged here or
anywhere downstream, and it never overwrites a name the customer already has saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

MAX_TEXT_LENGTH = 4096
MAX_ID_LENGTH = 200
_WA_ID_RE = re.compile(r"^\d{5,20}$")
STATUSES = ("sent", "delivered", "read", "failed")


@dataclass(frozen=True)
class InboundMessage:
    wa_message_id: str
    wa_id: str  # the customer's WhatsApp ID (phone number); never log it unmasked
    timestamp: datetime | None
    message_type: str  # "text", "interactive", "button" or "unsupported"
    text: str | None = None
    reply_id: str | None = None
    reply_title: str | None = None
    profile_name: str | None = None  # from contacts[].profile.name; never log it


@dataclass(frozen=True)
class StatusUpdate:
    wa_message_id: str
    status: str
    timestamp: datetime | None
    error_code: int | None = None


@dataclass(frozen=True)
class ParsedWebhook:
    messages: tuple[InboundMessage, ...] = ()
    statuses: tuple[StatusUpdate, ...] = ()
    ignored: int = 0


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _short(value, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _profile_names(value: dict) -> dict[str, str]:
    """Map ``wa_id`` -> contact profile name, from this payload's ``contacts`` list."""
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        wa_id = contact.get("wa_id")
        name = _short((contact.get("profile") or {}).get("name"), MAX_ID_LENGTH)
        if isinstance(wa_id, str) and name:
            names[wa_id] = name
    return names


def _message(data: dict, profile_names: dict[str, str] | None = None) -> InboundMessage | None:
    message_id = data.get("id")
    wa_id = data.get("from")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_ID_LENGTH:
        return None
    if not isinstance(wa_id, str) or not _WA_ID_RE.match(wa_id):
        return None
    kind = data.get("type")
    text = reply_id = reply_title = None
    if kind == "text":
        text = _short((data.get("text") or {}).get("body"), MAX_TEXT_LENGTH)
    elif kind == "interactive":
        interactive = data.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        reply_id = _short(reply.get("id"), MAX_ID_LENGTH)
        reply_title = _short(reply.get("title"), MAX_ID_LENGTH)
    elif kind == "button":  # quick-reply button on a template message
        button = data.get("button") or {}
        reply_id = _short(button.get("payload"), MAX_ID_LENGTH)
        reply_title = _short(button.get("text"), MAX_ID_LENGTH)
    else:
        kind = "unsupported"  # image, audio, location, sticker, ...
    return InboundMessage(
        wa_message_id=message_id,
        wa_id=wa_id,
        timestamp=_timestamp(data.get("timestamp")),
        message_type=kind,
        text=text,
        reply_id=reply_id,
        reply_title=reply_title,
        profile_name=(profile_names or {}).get(wa_id),
    )


def _status(data: dict) -> StatusUpdate | None:
    message_id = data.get("id")
    status = data.get("status")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_ID_LENGTH:
        return None
    if status not in STATUSES:
        return None
    error_code = None
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        error_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    return StatusUpdate(message_id, status, _timestamp(data.get("timestamp")), error_code)


def parse_webhook(payload: object, *, phone_number_id: str | None = None) -> ParsedWebhook:
    """Messages and statuses for ``phone_number_id`` (any number when it is ``None``)."""
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return ParsedWebhook(ignored=1)
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    ignored = 0
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            ignored += 1
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                ignored += 1
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                ignored += 1
                continue
            metadata = value.get("metadata") or {}
            if phone_number_id and metadata.get("phone_number_id") != phone_number_id:
                ignored += 1
                continue
            profile_names = _profile_names(value)
            for item in value.get("messages") or []:
                parsed = _message(item, profile_names) if isinstance(item, dict) else None
                if parsed:
                    messages.append(parsed)
                else:
                    ignored += 1
            for item in value.get("statuses") or []:
                parsed = _status(item) if isinstance(item, dict) else None
                if parsed:
                    statuses.append(parsed)
                else:
                    ignored += 1
    return ParsedWebhook(tuple(messages), tuple(statuses), ignored)

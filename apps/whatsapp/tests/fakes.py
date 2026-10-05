"""Test doubles and payload builders. Nothing here talks to Meta."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from apps.iva_common.http_transport import HttpResponse

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE_NUMBER_ID = "100000000000001"
# Unique per test run (keeps the "91" prefix and the "0001" ending the tests rely on), so
# leftover state from an earlier or interrupted run can never affect webhook tests.
RUN_TOKEN = f"{secrets.randbelow(10**6):06d}"
WA_ID = f"91{RUN_TOKEN}0001"
TOKEN = "test-access-token-never-real"

SETTINGS = {
    "WHATSAPP_APP_SECRET": APP_SECRET,
    "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
    "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    "WHATSAPP_ACCESS_TOKEN": TOKEN,
    "WHATSAPP_API_VERSION": "v21.0",
    "WHATSAPP_GRAPH_BASE_URL": "https://graph.example.invalid",
}


def ok(message_id: str = "wamid.OUT1") -> HttpResponse:
    body = {"messaging_product": "whatsapp", "messages": [{"id": message_id}]}
    return HttpResponse(200, json.dumps(body).encode())


def error(status: int, code: int = 131000, headers=None) -> HttpResponse:
    body = {"error": {"message": f"details for {WA_ID}", "code": code}}
    return HttpResponse(status, json.dumps(body).encode(), headers or {})


class FakeTransport:
    """Scripted responses (or exceptions); records every request."""

    def __init__(self, *responses, default=None):
        self.responses = list(responses)
        self.default = default
        self.requests: list[dict] = []
        self._counter = 0

    def request(self, method, url, *, headers=None, body=None, connect_timeout, read_timeout):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json": json.loads(body) if body else None,
                "timeouts": (connect_timeout, read_timeout),
            }
        )
        if self.responses:
            item = self.responses.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            self._counter += 1
            item = ok(f"wamid.OUT{self._counter}")
        if isinstance(item, Exception):
            raise item
        return item


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def text_message(body="hi", message_id="wamid.IN1", wa_id=WA_ID, ts="1760000000") -> dict:
    return {
        "from": wa_id,
        "id": message_id,
        "timestamp": ts,
        "type": "text",
        "text": {"body": body},
    }


def button_reply(reply_id, title="Tap", message_id="wamid.IN2", wa_id=WA_ID) -> dict:
    return {
        "from": wa_id,
        "id": message_id,
        "timestamp": "1760000001",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": reply_id, "title": title}},
    }


def list_reply(reply_id, title="Row", message_id="wamid.IN3", wa_id=WA_ID) -> dict:
    return {
        "from": wa_id,
        "id": message_id,
        "timestamp": "1760000002",
        "type": "interactive",
        "interactive": {
            "type": "list_reply",
            "list_reply": {"id": reply_id, "title": title, "description": "d"},
        },
    }


def status(message_id="wamid.OUT1", value="delivered", errors=None) -> dict:
    item = {"id": message_id, "status": value, "timestamp": "1760000100", "recipient_id": WA_ID}
    if errors:
        item["errors"] = errors
    return item


def payload(messages=(), statuses=(), phone_number_id=PHONE_NUMBER_ID) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Ravi Kumar"}, "wa_id": WA_ID}],
    }
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": value}]}],
    }


def encode(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")

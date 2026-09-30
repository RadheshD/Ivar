"""S5 end-to-end tests: signed webhook payloads through the real database, the real
Redis (session store, cache, per-customer lock) and an eager Celery task (the project's
established stand-in for "a real worker" in automated tests; a literal separate worker
process is exercised manually, see README.md), using the Fake Backend and no LLM
(rules/fuzzy only). One full customer journey per language/script variant: new user
onboarding, discovery, add to cart, checkout, payment pending -> paid, order status,
then a second, returning-user message that skips onboarding.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client, override_settings

from apps.conversations.adapters import get_backend
from apps.conversations.engine import reset_engine
from apps.conversations.state import store_from_settings
from apps.whatsapp import tasks
from apps.whatsapp.client import WhatsAppClient

from .fakes import SETTINGS, TOKEN, FakeTransport, encode, list_reply, payload, sign, text_message

pytestmark = pytest.mark.django_db

ADDRESS = "Flat 12, Green Park Road, Madhapur, Hyderabad 500081"

# One full-journey phrase set per variant. en/te/hi are plain per-language text;
# te_en/hi_en mix English nouns into Telugu/Hindi grammar (ARCHITECTURE.md's own
# examples, "naaku snacks kavali" / "mujhe protein chahiye", are this same style);
# te_roman is Telugu written in Latin script without English words.
VARIANTS = {
    "en": {
        "lang": "en",
        "search": "do you have makhana",
        "add": "add 2 makhana",
        "checkout": "checkout",
        "order": "where is my order",
    },
    "te": {
        "lang": "te",
        "search": "మఖానా ఉందా",
        "add": "రెండు ప్యాకెట్లు మఖానా జోడించు",
        "checkout": "ఆర్డర్ చేయి",
        "order": "నా ఆర్డర్ ఎక్కడ ఉంది",
    },
    "hi": {
        "lang": "hi",
        "search": "मखाना है क्या",
        "add": "दो पैकेट मखाना जोड़ो",
        "checkout": "ऑर्डर कर दो",
        "order": "मेरा ऑर्डर कहाँ है",
    },
    "te_en": {
        "lang": "te",
        "search": "naaku makhana kavali",
        "add": "add 2 makhana",
        "checkout": "order cheyyi",
        "order": "naaku order eppudu vastundi",
    },
    "hi_en": {
        "lang": "hi",
        "search": "mujhe makhana chahiye",
        "add": "add 2 makhana",
        "checkout": "order kardo",
        "order": "mera order kahan hai",
    },
    "te_roman": {
        "lang": "te",
        "search": "makhana unda",
        "add": "rendu makhana pettu",
        "checkout": "order pettu",
        "order": "naaku order ekkada undi",
    },
}


_WA_ID_SUFFIX = {name: str(i).rjust(4, "0") for i, name in enumerate(VARIANTS)}


def _wa_id(variant: str) -> str:
    # Fixed and distinct per variant, so real-Redis session state never crosses test
    # cases; ``_clean_redis_state`` below deletes each one before and after every run.
    return "9190000" + _WA_ID_SUFFIX[variant]


class WebhookChat:
    """One customer talking to the real webhook, through a real eager Celery task."""

    def __init__(self, wa_id: str, transport: FakeTransport):
        self.wa_id = wa_id
        self.transport = transport
        self._from = 0

    def _turn(self, message: dict) -> WebhookChat:
        self._from = len(self.transport.requests)
        body = encode(payload([message]))
        response = Client().post(
            "/webhooks/whatsapp/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        assert response.status_code == 200, response.content
        return self

    def send(self, text: str) -> WebhookChat:
        return self._turn(text_message(text, f"wamid.{uuid.uuid4().hex}", self.wa_id))

    def tap(self, reply_id: str, title: str = "Tap") -> WebhookChat:
        return self._turn(list_reply(reply_id, title, f"wamid.{uuid.uuid4().hex}", self.wa_id))

    @property
    def sent(self) -> list[dict]:
        """The Cloud API message bodies sent during the last turn, in order."""
        return [r["json"] for r in self.transport.requests[self._from :]]

    @property
    def body(self) -> str:
        parts = []
        for msg in self.sent:
            if msg["type"] == "text":
                parts.append(msg["text"]["body"])
            elif msg["type"] == "interactive":
                parts.append(msg["interactive"]["body"]["text"])
        return "\n".join(parts)

    @property
    def reply_ids(self) -> list[str]:
        ids = []
        for msg in self.sent:
            if msg.get("type") != "interactive":
                continue
            action = msg["interactive"]["action"]
            if msg["interactive"]["type"] == "button":
                ids.extend(b["reply"]["id"] for b in action["buttons"])
            else:
                ids.extend(row["id"] for section in action["sections"] for row in section["rows"])
        return ids


@pytest.fixture
def webhook(monkeypatch):
    """A real webhook + a real eager Celery task, with only the outbound Cloud API
    call faked (nothing ever calls Meta)."""
    transport = FakeTransport()

    def client():
        return WhatsAppClient(
            access_token=TOKEN,
            phone_number_id=SETTINGS["WHATSAPP_PHONE_NUMBER_ID"],
            api_version="v21.0",
            base_url="https://graph.example.invalid",
            transport=transport,
            sleep=lambda _s: None,
        )

    monkeypatch.setattr(tasks, "client_from_settings", client)
    reset_engine()
    with override_settings(**SETTINGS):
        yield transport
    reset_engine()


@pytest.fixture(autouse=True)
def _clean_redis_state():
    store = store_from_settings()
    for variant in VARIANTS:
        store.delete(_wa_id(variant))
    yield
    for variant in VARIANTS:
        store.delete(_wa_id(variant))


@pytest.mark.parametrize("variant", VARIANTS)
def test_full_journey_new_and_returning_customer(webhook, variant):
    p = VARIANTS[variant]
    wa_id = _wa_id(variant)
    chat = WebhookChat(wa_id, webhook)

    # --- new user: onboarding (language only) -------------------------------------
    chat.send("hi")
    assert any(rid.startswith("LANGUAGE_SELECT:") for rid in chat.reply_ids), chat.body
    chat.tap(f"LANGUAGE_SELECT:{p['lang']}")

    # --- discovery ---------------------------------------------------------------
    chat.send(p["search"])
    assert "makhana" in chat.body.lower() or "మఖానా" in chat.body or "मखाना" in chat.body, chat.body

    # --- add to cart ---------------------------------------------------------------
    chat.send(p["add"])

    # --- checkout with a new address ------------------------------------------------
    chat.send(p["checkout"])
    chat.send(ADDRESS)
    confirm = next(rid for rid in chat.reply_ids if rid.startswith("ACT:confirm"))
    chat.tap(confirm)
    body_after_confirm = chat.body

    backend = get_backend()
    customer = backend.get_or_create_customer(wa_id)
    [order] = backend.list_recent_orders(customer.id, limit=10)
    assert order.payment_status == "PENDING"
    assert order.order_number in body_after_confirm

    # --- payment pending -> paid ----------------------------------------------------
    backend.simulate_payment_confirmed(order.order_number)

    # --- order status ---------------------------------------------------------------
    chat.send(p["order"])
    assert order.order_number in chat.body

    # --- returning customer: onboarding is skipped ----------------------------------
    chat.send("hi")
    assert not any(rid.startswith("LANGUAGE_SELECT:") for rid in chat.reply_ids), chat.body

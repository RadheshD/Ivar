"""Engine -> renderer -> client, without a database."""

from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline
from apps.conversations.channel import Button, Inbound, OutProductCard
from apps.conversations.engine import ConversationEngine
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.state import InMemorySessionStore
from apps.whatsapp.client import WhatsAppClient
from apps.whatsapp.delivery import deliver, send_replies

from .fakes import PHONE_NUMBER_ID, TOKEN, WA_ID, FakeTransport, error


def make(transport):
    engine = ConversationEngine(
        FakeBackend(), InMemorySessionStore(), IntentPipeline(FakeLLMClient())
    )
    client = WhatsAppClient(
        access_token=TOKEN,
        phone_number_id=PHONE_NUMBER_ID,
        api_version="v21.0",
        base_url="https://graph.example.invalid",
        transport=transport,
        sleep=lambda _s: None,
    )
    return engine, client


def test_replies_are_rendered_and_sent_in_order():
    transport = FakeTransport()
    engine, client = make(transport)
    sent = deliver(WA_ID, Inbound.typed("hi"), engine=engine, client=client)
    assert [(s.template_key, s.message_type, s.ok) for s in sent] == [
        ("language.prompt_greeting_unnamed", "button", True)
    ]
    assert sent[0].wa_message_id == "wamid.OUT1"
    [request] = transport.requests
    assert request["json"]["to"] == WA_ID
    buttons = request["json"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == [
        "LANGUAGE_SELECT:en",
        "LANGUAGE_SELECT:te",
        "LANGUAGE_SELECT:hi",
    ]


def test_several_replies_become_several_messages():
    transport = FakeTransport()
    engine, client = make(transport)
    for inbound in (
        Inbound.typed("hi"),
        Inbound.tapped("LANGUAGE_SELECT:en"),
        Inbound.tapped("ACT:diet"),
    ):
        deliver(WA_ID, inbound, engine=engine, client=client)
    sent = deliver(WA_ID, Inbound.tapped("ACT:diet:veg"), engine=engine, client=client)
    assert [s.template_key for s in sent] == ["diet.saved", "menu.body"]
    assert [s.message_type for s in sent] == ["text", "list"]


def test_a_failed_send_stops_the_remaining_replies():
    transport = FakeTransport()
    engine, client = make(transport)
    for inbound in (
        Inbound.typed("hi"),
        Inbound.tapped("LANGUAGE_SELECT:en"),
        Inbound.tapped("ACT:diet"),
    ):
        deliver(WA_ID, inbound, engine=engine, client=client)
    transport.responses = [error(400)]
    sent = deliver(WA_ID, Inbound.tapped("ACT:diet:veg"), engine=engine, client=client)
    assert len(sent) == 1
    assert not sent[0].ok
    assert sent[0].error.status == 400


def _card(image_url="https://example.invalid/a.jpg"):
    add = Button("ACT:addcard:seed-mix", "Add to cart")
    order = Button("ACT:ordernow:seed-mix", "Order now")
    return OutProductCard("Seed Mix\n₹349", (add, order), image_url=image_url, key="product.card")


def test_image_send_failure_retries_once_without_the_image():
    transport = FakeTransport()
    transport.responses = [error(400, code=131053)]  # media download error, then success
    _, client = make(transport)
    sent = send_replies(WA_ID, [_card()], client=client)
    assert [(s.template_key, s.message_type, s.ok) for s in sent] == [
        ("product.card", "button", True)
    ]
    assert len(transport.requests) == 2
    assert "header" in transport.requests[0]["json"]["interactive"]
    assert "header" not in transport.requests[1]["json"]["interactive"]


def test_image_retry_also_failing_stops_delivery():
    transport = FakeTransport()
    transport.responses = [error(400), error(400)]
    _, client = make(transport)
    sent = send_replies(WA_ID, [_card(), _card(image_url=None)], client=client)
    assert len(sent) == 1
    assert not sent[0].ok
    assert len(transport.requests) == 2  # the original try plus the one retry; no loops


def test_card_without_an_image_is_not_retried_on_failure():
    transport = FakeTransport()
    transport.responses = [error(400)]
    _, client = make(transport)
    sent = send_replies(WA_ID, [_card(image_url=None)], client=client)
    assert len(sent) == 1
    assert not sent[0].ok
    assert len(transport.requests) == 1

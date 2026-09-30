"""Cloud API client with a fake transport: no network, no real credentials."""

import logging

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.conversations.channel import Button, OutButtons, OutList, OutText, Row
from apps.conversations.renderers import render
from apps.iva_common.http_transport import HttpError, HttpResponse
from apps.whatsapp.client import WhatsAppClient, WhatsAppSendError, client_from_settings

from .fakes import PHONE_NUMBER_ID, SETTINGS, TOKEN, WA_ID, FakeTransport, error, ok


def make_client(transport, **kwargs):
    sleeps = []
    client = WhatsAppClient(
        access_token=TOKEN,
        phone_number_id=PHONE_NUMBER_ID,
        api_version="v21.0",
        base_url="https://graph.example.invalid",
        transport=transport,
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


def test_request_shape():
    transport = FakeTransport(ok("wamid.A"))
    client, _ = make_client(transport)
    result = client.send(WA_ID, render(OutText("Hello")))
    assert result.wa_message_id == "wamid.A"
    [request] = transport.requests
    assert request["method"] == "POST"
    assert request["url"] == f"https://graph.example.invalid/v21.0/{PHONE_NUMBER_ID}/messages"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": WA_ID,
        "type": "text",
        "text": {"body": "Hello", "preview_url": False},
    }
    assert request["timeouts"] == (3.0, 10.0)


def test_renderer_to_api_mapping_for_interactive_messages():
    transport = FakeTransport()
    client, _ = make_client(transport)
    client.send(WA_ID, render(OutButtons("Pick", (Button("VIEW_CART", "View cart"),))))
    client.send(WA_ID, render(OutList("Choose", "Menu", (Row("HELP", "Help", "desc"),))))
    buttons, listing = (r["json"] for r in transport.requests)
    assert buttons["interactive"]["type"] == "button"
    assert buttons["interactive"]["action"]["buttons"][0]["reply"] == {
        "id": "VIEW_CART",
        "title": "View cart",
    }
    assert listing["interactive"]["action"]["sections"][0]["rows"][0]["id"] == "HELP"
    assert all(r["json"]["to"] == WA_ID for r in transport.requests)


def test_retries_429_honouring_retry_after():
    transport = FakeTransport(error(429, headers={"Retry-After": "2"}), ok())
    client, sleeps = make_client(transport)
    assert client.send(WA_ID, render(OutText("x"))).attempts == 2
    assert sleeps == [2.0]


def test_retries_5xx_with_exponential_backoff_then_succeeds():
    transport = FakeTransport(error(500), error(503), ok())
    client, sleeps = make_client(transport)
    assert client.send(WA_ID, render(OutText("x"))).attempts == 3
    assert sleeps == [0.5, 1.0]


def test_retries_transport_failures():
    transport = FakeTransport(HttpError("timeout"), HttpError("connection"), ok())
    client, sleeps = make_client(transport)
    assert client.send(WA_ID, render(OutText("x"))).attempts == 3
    assert len(sleeps) == 2


def test_gives_up_after_max_attempts():
    transport = FakeTransport(default=error(500))
    client, sleeps = make_client(transport, max_attempts=3)
    with pytest.raises(WhatsAppSendError) as info:
        client.send(WA_ID, render(OutText("x")))
    assert (info.value.kind, info.value.status, info.value.retryable) == ("http", 500, True)
    assert len(transport.requests) == 3
    assert len(sleeps) == 2


def test_backoff_is_capped():
    transport = FakeTransport(error(429, headers={"Retry-After": "600"}), ok())
    client, sleeps = make_client(transport)
    client.send(WA_ID, render(OutText("x")))
    assert sleeps == [8.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_is_not_retried(status):
    transport = FakeTransport(error(status, code=131047))
    client, sleeps = make_client(transport)
    with pytest.raises(WhatsAppSendError) as info:
        client.send(WA_ID, render(OutText("x")))
    assert (info.value.status, info.value.code, info.value.retryable) == (status, 131047, False)
    assert len(transport.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"messages": []}', b'{"messages": [{}]}'])
def test_success_without_a_message_id_is_invalid_and_not_resent(body):
    transport = FakeTransport(HttpResponse(200, body))
    client, _ = make_client(transport)
    with pytest.raises(WhatsAppSendError) as info:
        client.send(WA_ID, render(OutText("x")))
    assert info.value.kind == "invalid_response"
    assert len(transport.requests) == 1


def test_token_text_and_number_never_leak(caplog):
    transport = FakeTransport(default=error(500))
    client, _ = make_client(transport)
    with caplog.at_level(logging.DEBUG), pytest.raises(WhatsAppSendError) as info:
        client.send(WA_ID, render(OutText("secret message text")))
    exposed = "\n".join(r.getMessage() for r in caplog.records) + str(info.value) + repr(client)
    for private in (TOKEN, WA_ID, "secret message text"):
        assert private not in exposed
    assert "91******0001" in exposed  # masked recipient


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", ""),
        ("phone_number_id", ""),
        ("phone_number_id", "abc"),
        ("api_version", ""),
        ("api_version", "21"),
        ("base_url", "http://graph.example.invalid"),
    ],
)
def test_invalid_configuration(field, value):
    kwargs = {
        "access_token": TOKEN,
        "phone_number_id": PHONE_NUMBER_ID,
        "api_version": "v21.0",
        "base_url": "https://graph.example.invalid",
        field: value,
    }
    with pytest.raises(ImproperlyConfigured):
        WhatsAppClient(**kwargs)


def test_client_from_settings():
    with override_settings(**SETTINGS):
        client = client_from_settings(FakeTransport())
    assert client.url.startswith("https://graph.example.invalid/v21.0/")


def test_client_from_settings_without_credentials():
    with override_settings(WHATSAPP_ACCESS_TOKEN=""), pytest.raises(ImproperlyConfigured):
        client_from_settings(FakeTransport())

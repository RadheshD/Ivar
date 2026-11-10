"""The welcome banner delivered from a LOCAL file: uploaded to WhatsApp once, the media ID cached.

Every HTTP call is a fake: no real network. The banner file is a tiny temp file, never the real
one.
"""

import hashlib
import json
import logging

import pytest
from django.core.cache import cache

from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline
from apps.conversations.channel import WELCOME_BANNER_REF, Inbound, OutProductCard
from apps.conversations.engine import ConversationEngine
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.state import InMemorySessionStore
from apps.iva_common.http_transport import HttpError, HttpResponse
from apps.whatsapp import banner
from apps.whatsapp.client import WhatsAppClient
from apps.whatsapp.delivery import send_replies

from .fakes import PHONE_NUMBER_ID, TOKEN, WA_ID, FakeTransport, error, ok

OTHER_WA_ID = "919876500777"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-banner-bytes" * 50
JPEG = b"\xff\xd8\xff\xe0" + b"fake-banner-bytes" * 50
LINK = "https://cdn.example.invalid/ivar-welcome-banner.png"


def upload_ok(media_id="MEDIA1") -> HttpResponse:
    return HttpResponse(200, json.dumps({"id": media_id}).encode())


@pytest.fixture(autouse=True)
def _fresh_banner_state():
    banner._memo.clear()
    cache.clear()
    yield
    banner._memo.clear()
    cache.clear()


@pytest.fixture
def banner_file(tmp_path, settings):
    path = tmp_path / "banner.png"
    path.write_bytes(PNG)
    settings.IVA_WELCOME_BANNER_FILE = str(path)
    settings.IVA_WELCOME_BANNER_URL = ""
    return path


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


def first_message(engine, client, wa_id=WA_ID):
    replies = engine.handle(wa_id, Inbound(text="Hi", profile_name="Ravi"))
    return send_replies(wa_id, replies, client=client)


def is_upload(request) -> bool:
    return request["url"].endswith(f"/{PHONE_NUMBER_ID}/media")


def uploads(transport):
    return [r for r in transport.requests if is_upload(r)]


def messages(transport):
    return [r for r in transport.requests if not is_upload(r)]


def header_of(request):
    return request["json"]["interactive"].get("header")


def cache_key(data=PNG):
    return f"{banner.KEY_PREFIX}{PHONE_NUMBER_ID}:{hashlib.sha256(data).hexdigest()}"


# ------------------------------------------------------------------ the engine side


def test_the_engine_only_carries_the_abstract_banner_reference(banner_file):
    engine, _ = make(FakeTransport())
    [picker] = engine.handle(WA_ID, Inbound(text="Hi"))
    assert isinstance(picker, OutProductCard)
    assert picker.image_ref == WELCOME_BANNER_REF
    assert picker.image_url is None and picker.image_media_id is None


# ------------------------------------------------------------------ upload once, cache, reuse


def test_first_picker_uploads_once_caches_the_id_and_sends_the_header_with_it(banner_file):
    transport = FakeTransport(upload_ok("MEDIA1"))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [(s.template_key, s.ok) for s in sent] == [("language.prompt_greeting_named", True)]

    [upload] = uploads(transport)
    assert upload["method"] == "POST"
    assert upload["url"] == f"https://graph.example.invalid/v21.0/{PHONE_NUMBER_ID}/media"
    assert upload["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert upload["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert upload["timeouts"] == (3.0, 5.0)  # short: a customer is waiting
    body = upload["body"]
    assert b'name="messaging_product"' in body and b"whatsapp" in body
    assert b'name="type"' in body and b"image/png" in body
    assert b'name="file"; filename="banner.png"' in body
    assert PNG in body

    [message] = messages(transport)
    assert header_of(message) == {"type": "image", "image": {"id": "MEDIA1"}}
    assert cache.get(cache_key()) == "MEDIA1"


def test_the_cached_id_has_a_ttl_well_under_the_30_day_media_expiry(banner_file, monkeypatch):
    seen = {}
    real_set = cache.set
    monkeypatch.setattr(
        banner.cache,
        "set",
        lambda key, value, timeout=None: (seen.update(t=timeout), real_set(key, value, timeout)),
    )
    engine, client = make(FakeTransport(upload_ok()))
    first_message(engine, client)
    assert seen["t"] == 20 * 24 * 3600
    assert seen["t"] < 30 * 24 * 3600


def test_the_cache_key_holds_the_phone_number_id_and_the_content_hash(banner_file):
    engine, client = make(FakeTransport(upload_ok()))
    first_message(engine, client)
    assert cache_key() == f"iva:media:welcome:{PHONE_NUMBER_ID}:{hashlib.sha256(PNG).hexdigest()}"
    assert cache.get(cache_key()) == "MEDIA1"


def test_a_second_picker_makes_no_upload_call(banner_file):
    transport = FakeTransport(upload_ok("MEDIA1"))
    engine, client = make(transport)
    first_message(engine, client, WA_ID)
    first_message(engine, client, OTHER_WA_ID)  # another first-time customer
    assert len(uploads(transport)) == 1
    first, second = messages(transport)
    assert header_of(first) == header_of(second) == {"type": "image", "image": {"id": "MEDIA1"}}


def test_a_changed_file_is_a_new_cache_entry(banner_file):
    transport = FakeTransport(upload_ok("MEDIA1"), ok("wamid.A"), upload_ok("MEDIA2"))
    engine, client = make(transport)
    first_message(engine, client, WA_ID)
    banner_file.write_bytes(PNG + b"changed")
    first_message(engine, client, OTHER_WA_ID)
    assert len(uploads(transport)) == 2
    assert header_of(messages(transport)[1]) == {"type": "image", "image": {"id": "MEDIA2"}}


def test_a_jpeg_is_uploaded_with_its_mime_type(banner_file):
    banner_file.write_bytes(JPEG)
    transport = FakeTransport(upload_ok())
    engine, client = make(transport)
    first_message(engine, client)
    assert b"image/jpeg" in uploads(transport)[0]["body"]


# ------------------------------------------------------------------ upload problems


def assert_text_only_and_ok(transport, sent):
    assert [s.ok for s in sent] == [True]
    [message] = messages(transport)
    assert header_of(message) is None
    assert message["json"]["interactive"]["action"]["buttons"]  # the picker itself is intact


@pytest.mark.parametrize(
    "failure",
    [error(500), error(400), error(401), HttpError("timeout"), HttpError("connection"),
     HttpResponse(200, b"not json"), HttpResponse(200, b'{"id": ""}')],
)  # fmt: skip
def test_upload_failure_or_timeout_sends_the_picker_text_only(banner_file, failure):
    transport = FakeTransport(failure)
    engine, client = make(transport)
    sent = first_message(engine, client)  # must not raise
    assert len(uploads(transport)) == 1  # a single attempt, no retries
    assert_text_only_and_ok(transport, sent)
    assert cache.get(cache_key()) is None  # nothing cached from a failed upload


def test_a_failed_upload_is_tried_again_for_the_next_customer(banner_file):
    transport = FakeTransport(error(500), ok("wamid.A"), upload_ok("MEDIA1"))
    engine, client = make(transport)
    first_message(engine, client, WA_ID)
    first_message(engine, client, OTHER_WA_ID)
    assert len(uploads(transport)) == 2
    assert header_of(messages(transport)[1]) == {"type": "image", "image": {"id": "MEDIA1"}}


def test_an_unreachable_cache_does_not_break_the_picker(banner_file, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("cache down")

    monkeypatch.setattr(banner.cache, "get", boom)
    monkeypatch.setattr(banner.cache, "set", boom)
    transport = FakeTransport(upload_ok("MEDIA1"))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    assert header_of(messages(transport)[0]) == {"type": "image", "image": {"id": "MEDIA1"}}


def test_two_first_messages_may_both_upload_without_breaking(banner_file, monkeypatch):
    monkeypatch.setattr(banner.cache, "get", lambda key, default=None: None)  # both miss
    transport = FakeTransport(upload_ok("MEDIA1"), ok("wamid.A"), upload_ok("MEDIA2"))
    engine, client = make(transport)
    a = first_message(engine, client, WA_ID)
    b = first_message(engine, client, OTHER_WA_ID)
    assert [s.ok for s in a + b] == [True, True]
    assert len(uploads(transport)) == 2


def test_the_token_is_never_logged(banner_file, caplog):
    caplog.set_level(logging.DEBUG)
    transport = FakeTransport(error(500))
    engine, client = make(transport)
    first_message(engine, client)
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text


# ------------------------------------------------------------------ a rejected cached ID


def test_a_rejected_cached_id_is_re_uploaded_once_and_retried_once(banner_file):
    cache.set(cache_key(), "STALE", 1000)
    transport = FakeTransport(error(400, code=131053), upload_ok("FRESH"))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    first, second = messages(transport)
    assert header_of(first) == {"type": "image", "image": {"id": "STALE"}}
    assert header_of(second) == {"type": "image", "image": {"id": "FRESH"}}
    assert len(uploads(transport)) == 1
    assert cache.get(cache_key()) == "FRESH"


def test_if_the_re_upload_fails_the_picker_goes_out_text_only(banner_file):
    cache.set(cache_key(), "STALE", 1000)
    transport = FakeTransport(error(400), error(500))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    stale, text_only = messages(transport)
    assert header_of(stale)["image"]["id"] == "STALE"
    assert header_of(text_only) is None
    assert len(uploads(transport)) == 1  # one re-upload, no more
    assert cache.get(cache_key()) is None  # the bad ID is forgotten


def test_if_the_retry_with_the_new_id_also_fails_the_picker_goes_out_text_only(banner_file):
    cache.set(cache_key(), "STALE", 1000)
    transport = FakeTransport(error(400), upload_ok("FRESH"), error(400))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    stale, fresh, text_only = messages(transport)
    assert header_of(stale)["image"]["id"] == "STALE"
    assert header_of(fresh)["image"]["id"] == "FRESH"
    assert header_of(text_only) is None
    assert len(uploads(transport)) == 1  # still only one re-upload
    assert cache.get(cache_key()) is None


def test_a_server_error_with_a_cached_id_does_not_trigger_a_re_upload(banner_file):
    cache.set(cache_key(), "GOOD", 1000)
    transport = FakeTransport(error(500), error(500), error(500), error(500))
    engine, client = make(transport)
    first_message(engine, client)  # the send is retried by the client, then text-only; no raise
    assert uploads(transport) == []


def test_a_freshly_uploaded_id_that_fails_to_send_falls_back_to_text_only(banner_file):
    transport = FakeTransport(upload_ok("NEW"), error(400))
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    with_header, text_only = messages(transport)
    assert header_of(with_header)["image"]["id"] == "NEW"
    assert header_of(text_only) is None
    assert len(uploads(transport)) == 1  # a new ID is not uploaded twice


# ------------------------------------------------------------------ the file itself


def run_twice_and_expect_text_only(transport_responses=()):
    transport = FakeTransport(*transport_responses)
    engine, client = make(transport)
    first_message(engine, client, WA_ID)
    first_message(engine, client, OTHER_WA_ID)
    assert uploads(transport) == []
    assert all(header_of(m) is None for m in messages(transport))
    assert len(messages(transport)) == 2


def warnings_about_the_file(caplog):
    return [r for r in caplog.records if "welcome banner file not used" in r.getMessage()]


def test_a_missing_file_is_text_only_with_one_warning(tmp_path, settings, caplog):
    settings.IVA_WELCOME_BANNER_FILE = str(tmp_path / "nope.png")
    settings.IVA_WELCOME_BANNER_URL = ""
    caplog.set_level(logging.WARNING)
    run_twice_and_expect_text_only()
    assert len(warnings_about_the_file(caplog)) == 1


def test_a_file_that_is_not_png_or_jpeg_is_text_only_with_one_warning(tmp_path, settings, caplog):
    path = tmp_path / "banner.png"
    path.write_bytes(b"GIF89a" + b"x" * 100)  # a GIF named .png: the content decides
    settings.IVA_WELCOME_BANNER_FILE = str(path)
    settings.IVA_WELCOME_BANNER_URL = ""
    caplog.set_level(logging.WARNING)
    run_twice_and_expect_text_only()
    [record] = warnings_about_the_file(caplog)
    assert "PNG or JPEG" in record.getMessage()


def test_a_file_over_5_mb_is_text_only_with_one_warning(tmp_path, settings, caplog):
    path = tmp_path / "banner.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024))
    settings.IVA_WELCOME_BANNER_FILE = str(path)
    settings.IVA_WELCOME_BANNER_URL = ""
    caplog.set_level(logging.WARNING)
    run_twice_and_expect_text_only()
    [record] = warnings_about_the_file(caplog)
    assert "5 MB" in record.getMessage()


def test_a_file_just_under_5_mb_is_accepted(tmp_path, settings):
    path = tmp_path / "banner.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024 - 8))
    settings.IVA_WELCOME_BANNER_FILE = str(path)
    settings.IVA_WELCOME_BANNER_URL = ""
    transport = FakeTransport(upload_ok())
    engine, client = make(transport)
    first_message(engine, client)
    assert len(uploads(transport)) == 1


def test_no_file_configured_means_text_only_and_no_warning(settings, caplog):
    settings.IVA_WELCOME_BANNER_FILE = ""
    settings.IVA_WELCOME_BANNER_URL = ""
    caplog.set_level(logging.WARNING)
    run_twice_and_expect_text_only()
    assert warnings_about_the_file(caplog) == []


def test_a_relative_path_is_resolved_from_the_project_root(settings):
    settings.IVA_WELCOME_BANNER_FILE = "Reference/does-not-exist.png"
    path = banner._path()
    assert path.is_absolute()
    assert path.parent.name == "Reference"
    assert path.parent.parent == banner.Path(settings.BASE_DIR)


def test_test_settings_keep_the_banner_off_by_default():
    from django.conf import settings

    assert settings.IVA_WELCOME_BANNER_FILE == ""
    assert settings.IVA_WELCOME_BANNER_URL == ""


# ------------------------------------------------------------------ the public link still wins


def test_a_configured_link_keeps_the_link_behaviour_and_never_uploads(banner_file, settings):
    settings.IVA_WELCOME_BANNER_URL = LINK
    transport = FakeTransport()
    engine, client = make(transport)
    first_message(engine, client)
    assert uploads(transport) == []
    [message] = messages(transport)
    assert header_of(message) == {"type": "image", "image": {"link": LINK}}


def test_a_failed_link_still_falls_back_to_the_text_only_picker(banner_file, settings):
    settings.IVA_WELCOME_BANNER_URL = LINK
    transport = FakeTransport(error(400, code=131053))  # media download error, then success
    engine, client = make(transport)
    sent = first_message(engine, client)
    assert [s.ok for s in sent] == [True]
    first, second = messages(transport)
    assert header_of(first)["image"] == {"link": LINK}
    assert header_of(second) is None
    assert uploads(transport) == []


# ------------------------------------------------------------------ renderer


def test_a_media_id_wins_over_a_link_in_the_rendered_header():
    from apps.conversations.channel import Button
    from apps.conversations.renderers import render

    card = OutProductCard(
        "body",
        (Button("A", "A"),),
        image_url=LINK,
        image_media_id="MEDIA9",
    )
    header = render(card)["interactive"]["header"]
    assert header == {"type": "image", "image": {"id": "MEDIA9"}}
    assert render(OutProductCard("body", (Button("A", "A"),)))["interactive"].get("header") is None

"""A buttons message must not overtake the carousel / image card it follows."""

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from apps.conversations.channel import (
    Button,
    CarouselCard,
    OutButtons,
    OutCarousel,
    OutProductCard,
    OutText,
)
from apps.conversations.engine import reset_engine
from apps.whatsapp import ordering, tasks
from apps.whatsapp.client import WhatsAppClient, WhatsAppSendError
from apps.whatsapp.delivery import send_replies

from .fakes import (
    PHONE_NUMBER_ID,
    SETTINGS,
    TOKEN,
    WA_ID,
    FakeTransport,
    encode,
    error,
    ok,
    payload,
    sign,
    status,
    text_message,
)


def setup_function():
    cache.clear()


class Clock:
    """A fake monotonic clock whose ``sleep`` advances it (so no real waiting)."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


def wait(message_id="wamid.A", timeout=5.0, clock=None):
    clock = clock or Clock()
    return ordering.wait_for_status(message_id, timeout=timeout, sleep=clock.sleep, clock=clock)


# ------------------------------------------------------------------ the wait itself


def test_returns_at_once_when_the_status_already_arrived():
    ordering.note_status("wamid.A", "sent")
    clock = Clock()
    assert wait(clock=clock) is True
    assert clock.sleeps == []


def test_returns_as_soon_as_the_status_arrives_mid_wait():
    clock = Clock()
    clock.on_sleep = lambda now: ordering.note_status("wamid.A", "sent") if now >= 1.0 else None
    assert wait(clock=clock) is True
    assert clock.now == 1.0  # polled at 0.5 s steps and stopped at the first hit


def test_times_out_and_reports_false_without_exceeding_the_limit():
    clock = Clock()
    assert wait(timeout=5.0, clock=clock) is False
    assert clock.now == 5.0  # exactly the timeout, never more


def test_another_messages_status_does_not_end_the_wait():
    ordering.note_status("wamid.OTHER", "delivered")
    assert wait(timeout=1.0) is False


def test_a_later_status_also_satisfies_a_wait_for_sent():
    ordering.note_status("wamid.A", "read")
    assert wait() is True


@override_settings(IVA_ORDER_WAIT_STATUS="delivered")
def test_waiting_for_delivered_ignores_sent():
    ordering.note_status("wamid.A", "sent")
    assert wait(timeout=1.0) is False
    ordering.note_status("wamid.A", "delivered")
    assert wait(timeout=1.0) is True


def test_failed_and_unknown_statuses_are_not_recorded():
    ordering.note_status("wamid.A", "failed")
    ordering.note_status("wamid.A", "deleted")
    assert wait(timeout=1.0) is False


@override_settings(IVA_ORDER_WAIT_SECONDS=0)
def test_zero_turns_waiting_off():
    clock = Clock()
    assert ordering.wait_for_status("wamid.A", sleep=clock.sleep, clock=clock) is False
    assert clock.sleeps == []


@override_settings(IVA_ORDER_WAIT_SECONDS=2)
def test_the_configured_timeout_is_used_by_default():
    clock = Clock()
    assert ordering.wait_for_status("wamid.A", sleep=clock.sleep, clock=clock) is False
    assert clock.now == 2.0


def test_a_broken_cache_fails_open_instead_of_waiting(monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(ordering.cache, "get", boom)
    monkeypatch.setattr(ordering.cache, "set", boom)
    clock = Clock()
    assert wait(clock=clock) is False
    assert clock.sleeps == []
    ordering.note_status("wamid.A", "sent")  # must not raise


# ------------------------------------------------------------------ send_replies ordering


def _card(n, image=True):
    return OutProductCard(
        f"P{n}\n₹{n}",
        (Button(f"ACT:addcard:p{n}", "Add to cart"), Button(f"ACT:ordernow:p{n}", "Order now")),
        image_url=f"https://example.invalid/{n}.jpg" if image else None,
        key="product.card",
    )


def _carousel():
    cards = tuple(CarouselCard(c.image_url, c.body, c.buttons) for c in (_card(1), _card(2)))
    return OutCarousel("Here", cards, (OutText("Intro", "category.body"), _card(1), _card(2)), "c")


FOOTER = OutButtons("More?", (Button("ACT:more", "Show more"),), "cards.footer")


class Recorder:
    """A transport that logs sends and waits in one list, so their order is checkable."""

    def __init__(self, wait_result=True, fail_first=False):
        self.events: list[str] = []
        self.wait_result = wait_result
        self.transport = FakeTransport()
        if fail_first:
            self.transport.responses = [error(400)]
        real = self.transport.request

        def request(*args, **kwargs):
            response = real(*args, **kwargs)
            body = self.transport.requests[-1]["json"]
            kind = body.get("interactive", {}).get("type", body.get("type"))
            self.events.append(f"send:{kind}")
            return response

        self.transport.request = request
        self.client = WhatsAppClient(
            access_token=TOKEN,
            phone_number_id=PHONE_NUMBER_ID,
            api_version="v21.0",
            base_url="https://graph.example.invalid",
            transport=self.transport,
            sleep=lambda _s: None,
        )

    def waiter(self, message_id):
        self.events.append(f"wait:{message_id}")
        return self.wait_result


def test_the_footer_is_sent_only_after_the_carousel_status_arrived():
    rec = Recorder()
    sent = send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client, wait_for_status=rec.waiter)
    assert rec.events == ["send:carousel", "wait:wamid.OUT1", "send:button"]
    assert [s.ok for s in sent] == [True, True]


def test_a_timeout_still_sends_the_footer():
    rec = Recorder(wait_result=False)
    sent = send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client, wait_for_status=rec.waiter)
    assert rec.events == ["send:carousel", "wait:wamid.OUT1", "send:button"]
    assert [s.template_key for s in sent] == ["c", "cards.footer"]
    assert all(s.ok for s in sent)  # the follow-up is delayed, never lost


def test_an_image_card_followed_by_buttons_waits():
    rec = Recorder()
    send_replies(WA_ID, [_card(1), FOOTER], client=rec.client, wait_for_status=rec.waiter)
    assert rec.events == ["send:button", "wait:wamid.OUT1", "send:button"]


def test_fallback_cards_wait_once_before_the_footer_not_between_cards():
    rec = Recorder(fail_first=True)  # the carousel is rejected, its cards go out instead
    send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client, wait_for_status=rec.waiter)
    assert rec.events == [
        "send:carousel",  # rejected
        "send:text",
        "send:button",
        "send:button",
        "wait:wamid.OUT3",  # the last card, then the footer
        "send:button",
    ]


def test_no_wait_for_a_text_only_card_or_a_light_message():
    rec = Recorder()
    send_replies(
        WA_ID,
        [_card(1, image=False), FOOTER, FOOTER],
        client=rec.client,
        wait_for_status=rec.waiter,
    )
    assert not any(e.startswith("wait") for e in rec.events)


def test_no_wait_when_a_large_message_is_last_or_followed_by_another_large_message():
    rec = Recorder()
    send_replies(WA_ID, [_carousel()], client=rec.client, wait_for_status=rec.waiter)
    send_replies(WA_ID, [_carousel(), _carousel()], client=rec.client, wait_for_status=rec.waiter)
    assert not any(e.startswith("wait") for e in rec.events)


def test_a_text_message_after_a_large_message_waits_too():
    # The "nutrition values are not verified" note is plain text: it must not overtake the carousel.
    rec = Recorder()
    send_replies(
        WA_ID, [_carousel(), OutText("note", "product.unverified")],
        client=rec.client, wait_for_status=rec.waiter,
    )  # fmt: skip
    assert rec.events[0] == "send:carousel" and rec.events[1].startswith("wait")
    assert rec.events[-1].startswith("send:")


def test_a_failed_large_send_does_not_wait():
    rec = Recorder()
    rec.transport.responses = [error(400), error(400)]  # carousel and its first fallback fail
    sent = send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client, wait_for_status=rec.waiter)
    assert not any(e.startswith("wait") for e in rec.events)
    assert not sent[-1].ok


def test_without_a_waiter_sends_are_unchanged():
    rec = Recorder()
    send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client)
    assert rec.events == ["send:carousel", "send:button"]


def test_a_waiter_that_raises_is_not_swallowed_silently():
    # The real waiter never raises (it fails open); a broken one must not hide behind a
    # swallowed error, so the exception propagates and the stalled-message sweep reports it.
    rec = Recorder()

    def broken(_id):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        send_replies(WA_ID, [_carousel(), FOOTER], client=rec.client, wait_for_status=broken)


def test_send_error_type_is_still_exported():
    assert issubclass(WhatsAppSendError, Exception)


# ------------------------------------------------------------------ webhook + worker wiring


@pytest.mark.django_db
def test_the_webhook_records_status_callbacks_for_the_waiting_worker():
    data = payload(statuses=[status("wamid.CAR1", "sent")])
    body = encode(data)
    with override_settings(**SETTINGS):
        response = Client().post(
            "/webhooks/whatsapp/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
    assert response.status_code == 200
    assert wait("wamid.CAR1", timeout=1.0) is True  # even though no outbound row exists


@pytest.mark.django_db
def test_the_worker_passes_the_waiter_to_send_replies(monkeypatch):
    seen = {}

    def fake_send_replies(wa_id, replies, *, client, wait_for_status=None):
        seen["waiter"] = wait_for_status
        return []

    monkeypatch.setattr(tasks, "send_replies", fake_send_replies)
    monkeypatch.setattr(
        tasks,
        "client_from_settings",
        lambda: WhatsAppClient(
            access_token=TOKEN,
            phone_number_id=PHONE_NUMBER_ID,
            api_version="v21.0",
            base_url="https://graph.example.invalid",
            transport=FakeTransport(default=ok()),
            sleep=lambda _s: None,
        ),
    )
    reset_engine()
    body = encode(payload([text_message("hi", "wamid.ORD1")]))
    with override_settings(**SETTINGS):
        Client().post(
            "/webhooks/whatsapp/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
    reset_engine()
    assert seen["waiter"] is ordering.wait_for_status

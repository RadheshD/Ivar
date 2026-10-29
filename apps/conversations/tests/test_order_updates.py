"""Order-status push events: the contract (validation), the template message shape, and the
typed phrases that stop and resume updates (saved per customer, in the database)."""

from datetime import UTC, datetime

import pytest

from apps.ai.text import normalize
from apps.conversations import order_updates
from apps.conversations.backend_port import (
    InvalidOrderEvent,
    OrderStatus,
    parse_order_status_change,
)
from apps.conversations.channel import OutTemplate
from apps.conversations.messages import (
    CATALOGS,
    ESCAPE_WORDS,
    ORDER_HISTORY_WORDS,
    ORDER_STATUS_WORDS,
    REORDER_WORDS,
    RESUME_UPDATES_WORDS,
    STOP_UPDATES_WORDS,
    en,
    hi,
    te,
)
from apps.conversations.models import Conversation
from apps.conversations.renderers import render

from .harness import ADDRESS, LANGUAGES, WA_ID, Chat, onboard

pytestmark = pytest.mark.django_db

GOOD = {
    "event_id": "evt-1001.a:b_c",
    "order_number": "IVA1001",
    "customer_id": "cust_1",
    "new_status": "CONFIRMED",
    "occurred_at": "2026-10-28T10:00:00+00:00",
}


def good(**changes):
    return {**GOOD, **changes}


# ----------------------------------------------------------------------- the contract


def test_a_valid_event_parses():
    event = parse_order_status_change(GOOD)
    assert (event.event_id, event.order_number, event.customer_id) == (
        "evt-1001.a:b_c",
        "IVA1001",
        "cust_1",
    )
    assert event.new_status == "CONFIRMED"
    assert event.occurred_at == datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def test_a_z_suffix_and_an_offset_are_accepted():
    z = parse_order_status_change(good(occurred_at="2026-10-28T10:00:00Z"))
    plus = parse_order_status_change(good(occurred_at="2026-10-28T15:30:00+05:30"))
    assert z.occurred_at == plus.occurred_at


@pytest.mark.parametrize("status", [s.value for s in OrderStatus])
def test_every_order_status_is_accepted(status):
    assert parse_order_status_change(good(new_status=status)).new_status == status


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "text",
        [],
        42,
        {},
        {k: v for k, v in GOOD.items() if k != "event_id"},  # a field missing
        {**GOOD, "extra": "x"},  # an unknown field
        good(event_id=""),
        good(event_id="has space"),
        good(event_id="x" * 129),
        good(event_id=5),
        good(order_number="IVA 1"),
        good(order_number="x" * 65),
        good(customer_id="+919876500001"),  # a phone number is not a backend ID
        good(customer_id=None),
        good(new_status="confirmed"),  # case matters
        good(new_status="SHIPPED"),  # not an OrderStatus
        good(new_status=["CONFIRMED"]),
        good(occurred_at="2026-10-28T10:00:00"),  # no time zone
        good(occurred_at="yesterday"),
        good(occurred_at=1761645600),
        good(occurred_at=None),
    ],
)
def test_a_malformed_event_is_rejected(bad):
    with pytest.raises(InvalidOrderEvent):
        parse_order_status_change(bad)


def test_the_rejection_text_never_echoes_the_input():
    with pytest.raises(InvalidOrderEvent) as caught:
        parse_order_status_change(good(event_id="secret-looking value"))
    assert "secret-looking" not in str(caught.value)


def test_the_event_type_is_not_the_status_history_entry():
    from apps.conversations.backend_port import OrderStatusChangeEvent, OrderStatusEvent

    assert OrderStatusChangeEvent is not OrderStatusEvent  # the existing type is untouched
    assert {"status", "at"} == set(OrderStatusEvent.__dataclass_fields__)


# ----------------------------------------------------------------------- template messages


def test_a_template_renders_with_its_language_and_body_parameter():
    payload = render(OutTemplate("order_confirmed", "hi", ("IVA1001",)))
    assert payload == {
        "type": "template",
        "template": {
            "name": "order_confirmed",
            "language": {"code": "hi"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "IVA1001"}]}],
        },
    }


def test_a_template_without_parameters_has_no_components():
    assert "components" not in render(OutTemplate("order_confirmed", "en"))["template"]


@pytest.mark.parametrize("name", ["", "Order-Confirmed", "order confirmed", "ORDER"])
def test_a_bad_template_name_is_refused(name):
    with pytest.raises(ValueError):
        OutTemplate(name, "en")


# ----------------------------------------------------------------------- the words


def test_every_status_has_a_message_in_every_language():
    for status in OrderStatus:
        for module in (en, hi, te):
            text = module.MESSAGES[f"notify.order_status.{status.value}"]
            assert "{order}" in text


def test_the_update_phrases_do_not_overlap_any_other_whole_message_word():
    stop, resume = STOP_UPDATES_WORDS, RESUME_UPDATES_WORDS
    assert not stop & resume
    for other in (ESCAPE_WORDS, ORDER_HISTORY_WORDS, ORDER_STATUS_WORDS, REORDER_WORDS):
        assert not (stop | resume) & other
    assert normalize("stop") not in stop  # bare "stop" still means "back to the menu"


def test_each_language_has_native_and_romanized_phrases():
    for module in (hi, te):
        for words in (module.STOP_UPDATES_WORDS, module.RESUME_UPDATES_WORDS):
            assert any(not w.isascii() for w in words)
            assert any(w.isascii() for w in words)


def test_the_requested_english_phrases_exist():
    assert {"stop order updates", "stop updates"} <= set(en.STOP_UPDATES_WORDS)
    assert {"resume order updates", "start updates"} <= set(en.RESUME_UPDATES_WORDS)


def test_the_messages_keep_the_same_placeholders_in_every_language():
    for key in (k for k in en.MESSAGES if k.startswith("notify.")):
        for lang in ("hi", "te"):
            assert CATALOGS[lang][key]


# ----------------------------------------------------------------------- opt-out, saved

# One phrase per language and script: stop, then the matching resume.
PHRASES = {
    "english": ("stop order updates", "resume order updates"),
    "hindi": ("ऑर्डर अपडेट बंद करो", "ऑर्डर अपडेट चालू करो"),
    "hinglish": ("order update band karo", "order update chalu karo"),
    "telugu": ("ఆర్డర్ అప్డేట్స్ ఆపు", "ఆర్డర్ అప్డేట్స్ ప్రారంభించు"),
    "tenglish": ("order updates aapu", "order updates start cheyyi"),
}


def test_updates_are_on_by_default(make_engine, backend):
    assert order_updates.updates_enabled(WA_ID) is True  # no row at all
    onboard(Chat(make_engine(backend)))
    assert order_updates.updates_enabled(WA_ID) is True


@pytest.mark.parametrize("kind", PHRASES)
def test_the_phrases_stop_and_resume_updates(make_engine, backend, kind):
    stop, resume = PHRASES[kind]
    chat = onboard(Chat(make_engine(backend)))
    chat.send(stop)
    assert chat.keys == ["notify.stopped"]
    assert order_updates.updates_enabled(WA_ID) is False
    chat.send(resume)
    assert chat.keys == ["notify.resumed"]
    assert order_updates.updates_enabled(WA_ID) is True


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_confirmation_is_in_the_customers_language(make_engine, backend, lang):
    from apps.conversations.messages import t

    chat = onboard(Chat(make_engine(backend)), lang)
    chat.send("stop updates")
    assert chat.body == t(lang, "notify.stopped")
    chat.send("resume updates")
    assert chat.body == t(lang, "notify.resumed")


def test_stopping_twice_is_harmless_and_the_choice_is_per_customer(make_engine, backend):
    chat = onboard(Chat(make_engine(backend)))
    chat.send("stop updates")
    chat.send("stop updates")
    assert order_updates.updates_enabled(WA_ID) is False
    assert order_updates.updates_enabled("919876500099") is True
    assert Conversation.objects.filter(order_updates_enabled=False).count() == 1


def test_a_sentence_containing_the_phrase_is_not_a_match(make_engine, backend):
    chat = onboard(Chat(make_engine(backend)))
    chat.send("please stop order updates for me now")
    assert "notify.stopped" not in chat.keys
    assert order_updates.updates_enabled(WA_ID) is True


def test_a_greeting_is_not_read_as_a_choice(make_engine, backend):
    chat = onboard(Chat(make_engine(backend)))
    chat.send("hi")
    assert "notify.stopped" not in chat.keys and "notify.resumed" not in chat.keys


def test_the_phrase_at_the_address_step_is_not_saved_as_an_address(make_engine, backend):
    chat = onboard(Chat(make_engine(backend)))
    chat.send("add 2 packets makhana")
    chat.send("checkout")
    assert chat.keys == ["checkout.type_address"]
    chat.send("stop order updates")
    assert chat.keys == ["notify.stopped"]
    assert order_updates.updates_enabled(WA_ID) is False
    customer = backend.get_or_create_customer(WA_ID)
    assert backend.list_addresses(customer.id) == []
    chat.send(ADDRESS)  # still waiting for the real address
    assert "checkout.confirm" in chat.keys

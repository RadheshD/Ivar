from datetime import UTC, datetime

import pytest

from apps.whatsapp.parser import parse_webhook
from apps.whatsapp.security import verify_signature, verify_token

from .fakes import (
    APP_SECRET,
    PHONE_NUMBER_ID,
    WA_ID,
    button_reply,
    list_reply,
    payload,
    sign,
    status,
    text_message,
)

BODY = b'{"object": "whatsapp_business_account"}'


# --------------------------------------------------------------------------- signature


def test_valid_signature():
    assert verify_signature(APP_SECRET, BODY, sign(BODY))
    assert verify_signature(APP_SECRET, BODY, sign(BODY).upper().replace("SHA256=", "sha256="))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha1=abc",
        sign(BODY, "other-secret"),
        sign(BODY + b" "),
        "sha256=zzzz",
        "sha256=ñññ",
    ],
)
def test_invalid_signature(header):
    assert not verify_signature(APP_SECRET, BODY, header)


def test_empty_secret_never_matches():
    assert not verify_signature("", BODY, sign(BODY, ""))
    assert not verify_signature(None, BODY, sign(BODY))


def test_signature_is_over_the_raw_bytes():
    reordered = b'{ "object":"whatsapp_business_account" }'
    assert not verify_signature(APP_SECRET, reordered, sign(BODY))


@pytest.mark.parametrize(
    ("expected", "given", "ok"),
    [("tok", "tok", True), ("tok", "TOK", False), ("tok", None, False), ("", "", False),
     ("tok", "tók", False)],
)  # fmt: skip
def test_verify_token(expected, given, ok):
    assert verify_token(expected, given) is ok


# --------------------------------------------------------------------------- parser


def test_text_message():
    parsed = parse_webhook(payload([text_message("  add 2 makhana  ")]), phone_number_id=None)
    [message] = parsed.messages
    assert message.wa_message_id == "wamid.IN1"
    assert message.wa_id == WA_ID
    assert message.message_type == "text"
    assert message.text == "add 2 makhana"
    assert message.timestamp == datetime.fromtimestamp(1760000000, tz=UTC)
    assert parsed.ignored == 0


def test_button_and_list_replies():
    parsed = parse_webhook(
        payload([button_reply("VIEW_CART", "View cart"), list_reply("PRODUCT_SELECT:oats")])
    )
    button, row = parsed.messages
    assert (button.message_type, button.reply_id, button.reply_title) == (
        "interactive",
        "VIEW_CART",
        "View cart",
    )
    assert row.reply_id == "PRODUCT_SELECT:oats"
    assert button.text is None


def test_template_quick_reply_button():
    data = {
        "from": WA_ID,
        "id": "wamid.B",
        "timestamp": "1",
        "type": "button",
        "button": {"payload": "HELP", "text": "Help"},
    }
    [message] = parse_webhook(payload([data])).messages
    assert (message.message_type, message.reply_id) == ("button", "HELP")


def test_unsupported_types_are_kept_as_unsupported():
    data = {"from": WA_ID, "id": "wamid.IMG", "timestamp": "1", "type": "image", "image": {}}
    [message] = parse_webhook(payload([data])).messages
    assert message.message_type == "unsupported"
    assert message.text is None


def test_statuses():
    failed = status("wamid.OUT2", "failed", errors=[{"code": 131026, "title": "x"}])
    parsed = parse_webhook(payload(statuses=[status(), failed, status("x", "deleted")]))
    first, second = parsed.statuses
    assert (first.wa_message_id, first.status, first.error_code) == (
        "wamid.OUT1",
        "delivered",
        None,
    )
    assert (second.status, second.error_code) == ("failed", 131026)
    assert parsed.ignored == 1  # unknown status value


def test_other_phone_number_is_ignored():
    parsed = parse_webhook(
        payload([text_message()], phone_number_id="999"), phone_number_id=PHONE_NUMBER_ID
    )
    assert parsed.messages == ()
    assert parsed.ignored == 1


def test_profile_name_is_read_and_matched_by_wa_id():
    [message] = parse_webhook(payload([text_message()])).messages
    assert message.profile_name == "Ravi Kumar"


def test_profile_name_is_none_when_contacts_is_missing_or_unmatched():
    payload_without_contacts = payload([text_message()])
    payload_without_contacts["entry"][0]["changes"][0]["value"]["contacts"] = []
    [message] = parse_webhook(payload_without_contacts).messages
    assert message.profile_name is None


def test_long_text_is_truncated():
    [message] = parse_webhook(payload([text_message("x" * 5000)])).messages
    assert len(message.text) == 4096


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "text",
        {"object": "page"},
        {"object": "whatsapp_business_account", "entry": "x"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "other"}]}]},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages"}]}]},
        payload([{"id": "wamid.X", "from": "not-a-number", "type": "text"}]),
        payload([{"from": WA_ID, "type": "text"}]),
        payload(["not a dict"]),
        payload(statuses=[{"status": "read"}]),
    ],
)
def test_malformed_payloads_never_raise(bad):
    parsed = parse_webhook(bad)
    assert parsed.messages == () and parsed.statuses == ()


def test_bad_timestamp_is_none():
    [message] = parse_webhook(payload([text_message(ts="soon")])).messages
    assert message.timestamp is None

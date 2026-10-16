"""Opening and closing messages: no-name greetings, the language confirmation at onboarding, the
thank-you after an order, no "Payment status" button on a paid order, hints on dead ends, and one
spelling of "menu" (Hindi) and "checkout" (Telugu)."""

import re

import pytest

from apps.conversations.channel import OutButtons, OutText
from apps.conversations.messages import CATALOGS, en, hi, t, te

from .harness import LANGUAGES, OUTAGE, WA_ID, Chat, checkout_to_payment, in_script, onboard

STRAY = re.compile(r"\s!|,\s*!")  # "Hi !" or "Welcome back, !"

# ------------------------------------------------------------------ B1: no name, no stray "!"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_greeting_without_a_name_has_no_stray_space_or_comma(chat, lang):
    onboard(chat, lang, name=None)
    chat.send("hi")
    assert chat.keys == ["greeting_noname", "menu.body"]
    assert chat.message("greeting_noname").body == t(lang, "greeting_noname")
    assert not STRAY.search(chat.message("greeting_noname").body)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_welcome_back_without_a_name_has_no_stray_space_or_comma(engine, store, lang):
    chat = onboard(Chat(engine), lang, name=None)
    store.delete(WA_ID)  # a new conversation, days later
    chat.send("hi")
    assert chat.keys == ["welcome_back_noname", "menu.body"]
    assert chat.message("welcome_back_noname").body == t(lang, "welcome_back_noname")
    assert not STRAY.search(chat.message("welcome_back_noname").body)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_greeting_and_welcome_back_with_a_name_are_unchanged(engine, store, lang):
    chat = onboard(Chat(engine), lang, name="Ravi")
    chat.send("hi")
    assert chat.keys == ["greeting", "menu.body"]
    assert chat.message("greeting").body == t(lang, "greeting", name="Ravi")
    store.delete(WA_ID)
    chat.send("hi")
    assert chat.keys == ["welcome_back", "menu.body"]
    assert chat.message("welcome_back").body == t(lang, "welcome_back", name="Ravi")


# ------------------------------------------------------------------ B2: language confirmation


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_first_language_choice_is_confirmed_in_that_language(chat, lang):
    chat.send("hi")
    chat.tap(f"LANGUAGE_SELECT:{lang}")
    assert chat.keys == ["language.changed", "menu.body"]
    assert chat.message("language.changed").body == t(lang, "language.changed")
    assert in_script(chat.message("menu.body").body, lang)


def test_the_confirmation_comes_before_the_planner_intro(chat, settings):
    settings.IVA_PLANNER_URL = "https://planner.test.invalid/plan"
    chat.send("hi")
    chat.tap("LANGUAGE_SELECT:hi")
    assert chat.keys == ["language.changed", "welcome.planner_intro", "welcome.browse_prompt"]


def test_an_unclear_reply_to_the_language_question_is_not_confirmed(chat):
    chat.send("hello")
    chat.send("hmm")
    assert chat.keys == ["language.prompt"]  # asked again, nothing confirmed yet


def test_changing_the_language_later_is_still_confirmed_once(chat):
    onboard(chat, "en")
    chat.tap("LANGUAGE_SELECT:te")
    assert chat.keys == ["language.changed", "menu.body"]


# ------------------------------------------------------------------ B3: thank you after an order


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_placed_order_says_thank_you_in_every_language(chat, lang):
    onboard(chat, lang)
    checkout_to_payment(chat, lang)
    lines = chat.message("checkout.created").body.split("\n")
    assert lines[1] == t(lang, "checkout.thanks")  # right after the "order placed" line
    assert in_script(lines[1], lang)
    assert len(lines) >= 4  # created, thanks, payment line, then the pay link or the no-link note


# --------------------------------------------------------------- B4: paid order, no Payment status


def _button_titles(chat):
    return [b.title for b in chat.message("order.status").buttons]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_paid_order_does_not_offer_payment_status(chat, backend, lang):
    onboard(chat, lang)
    order = checkout_to_payment(chat, lang)
    backend.simulate_payment_confirmed(order)
    chat.send(CATALOG_PHRASE[lang])
    assert chat.keys == ["order.status"]
    assert t(lang, "btn.payment_status") not in _button_titles(chat)
    assert _button_titles(chat) == [t(lang, "btn.menu")]  # the other button stays


CATALOG_PHRASE = {"en": "where is my order", "te": "నా ఆర్డర్ ఎక్కడ ఉంది", "hi": "मेरा ऑर्डर कहाँ है"}


def test_an_unpaid_order_still_offers_payment_status(chat):
    onboard(chat)
    checkout_to_payment(chat)
    chat.send("where is my order")
    assert _button_titles(chat) == [t("en", "btn.payment_status"), t("en", "btn.menu")]


# ------------------------------------------------------------------ B5: dead ends


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_fallback_gives_a_one_line_hint(chat, lang):
    onboard(chat, lang)
    chat.send("qwerty asdfgh")
    assert chat.keys == ["fallback"]
    message = chat.message("fallback")
    assert message.body == f"{t(lang, 'fallback')}\n{t(lang, 'fallback.hint')}"
    assert "\n" not in t(lang, "fallback.hint")  # the hint is one line
    assert [b.title for b in message.buttons] == [t(lang, "btn.menu"), t(lang, "btn.help")]


def test_the_service_busy_reply_has_a_menu_button(chat, flaky):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:packaged")
    flaky.fail["list_products"] = OUTAGE
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    message = chat.message("error.unavailable")
    assert isinstance(message, OutButtons)
    assert [b.title for b in message.buttons] == [t("en", "btn.menu")]
    assert message.body == t("en", "error.unavailable")


def test_the_service_busy_reply_without_a_customer_still_has_a_menu_button(chat, flaky):
    flaky.fail["get_or_create_customer"] = OUTAGE
    chat.send("hi")
    message = chat.message("error.unavailable")
    assert isinstance(message, OutButtons) and len(message.buttons) == 1


def test_the_invalid_quantity_reply_has_a_menu_button(chat):
    onboard(chat)
    chat.send("add 25 makhana")
    message = chat.message("qty.invalid")
    assert isinstance(message, OutButtons)
    assert [b.title for b in message.buttons] == [t("en", "btn.menu")]


def test_the_rate_limit_reply_gets_no_button(engine):
    reply = engine.rate_limited_reply(WA_ID)
    assert [m.key for m in reply] == ["limit.rate_limited"]
    assert isinstance(reply[0], OutText) and not isinstance(reply[0], OutButtons)


# ------------------------------------------------------------------ B6: one spelling


def _message_texts(module):
    return " ".join(module.MESSAGES.values())


def test_hindi_messages_use_one_spelling_of_menu():
    text = _message_texts(hi)
    assert "मेन्यू" in text
    assert "मेनू" not in text


def test_telugu_messages_use_one_spelling_of_checkout():
    text = _message_texts(te)
    assert "చెక్అవుట్" in text
    assert "చెక్‌అవుట్" not in text and "చెకౌట్" not in text


@pytest.mark.parametrize("word", ["मेनू", "मेन्यू"])
def test_both_hindi_spellings_still_leave_the_address_step(chat, word):
    onboard(chat, "hi")
    chat.send("add 1 makhana").send("checkout").send(word)
    assert chat.keys == ["menu.body"], word


# ------------------------------------------------------------------ all three catalogs agree


def test_en_hi_te_have_identical_key_sets():
    assert set(en.MESSAGES) == set(hi.MESSAGES) == set(te.MESSAGES)
    for key in (
        "greeting_noname",
        "welcome_back_noname",
        "checkout.thanks",
        "fallback.hint",
        "checkout.address_unclear",
    ):
        assert all(CATALOGS[lang][key].strip() for lang in ("en", "hi", "te")), key

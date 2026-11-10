"""Transcripts: the first-time language picker, its banner, the one-message confirmation, and
THANKS / BYE / ACK while the language is still unset."""

import pytest

from apps.conversations.channel import OutButtons, OutProductCard
from apps.conversations.messages import t

from .harness import WA_ID, onboard

BANNER = "https://cdn.example.invalid/ivar-welcome-banner.png"
LINE2 = "Choose your language · భాషను ఎంచుకోండి · भाषा चुनें"


def language_of(chat):
    return chat.engine.backend.get_or_create_customer(WA_ID).language


def titles(message):
    return [b.title for b in message.buttons]


# ------------------------------------------------------------------ the first-time picker


def test_picker_with_a_profile_name(chat):
    chat.send("Hi", profile_name="Ravi")
    assert chat.keys == ["language.prompt_greeting_named"]
    [picker] = chat.last
    assert isinstance(picker, OutProductCard)  # a reply-button message that can carry the banner
    assert picker.body == f"Hi Ravi, welcome to Ivar.\n{LINE2}"
    assert titles(picker) == ["English", "తెలుగు", "हिन्दी"]
    assert chat.ids == ["LANGUAGE_SELECT:en", "LANGUAGE_SELECT:te", "LANGUAGE_SELECT:hi"]


@pytest.mark.parametrize("name", [None, "", "   ", "🙂", "✨✨ 🎉", "***", "👍🏽"])
def test_picker_drops_a_missing_or_symbol_only_name(chat, name):
    chat.send("Hi", profile_name=name)
    assert chat.keys == ["language.prompt_greeting_unnamed"]
    assert chat.last[0].body == f"Hi, welcome to Ivar.\n{LINE2}"


@pytest.mark.parametrize("name", ["Ravi", "రవి", "रवि", "Ravi 🙂", "A1"])
def test_picker_keeps_a_name_that_has_letters(chat, name):
    chat.send("Hi", profile_name=name)
    assert chat.last[0].body.startswith(f"Hi {name}, welcome to Ivar.")


def test_picker_body_and_titles_fit_whatsapp_limits(chat):
    from apps.conversations.renderers import BUTTON_TITLE_MAX, INTERACTIVE_BODY_MAX

    chat.send("Hi", profile_name="R" * 60)
    [picker] = chat.last
    assert len(picker.body) <= INTERACTIVE_BODY_MAX
    assert all(len(b.title) <= BUTTON_TITLE_MAX for b in picker.buttons)
    assert len(picker.buttons) == 3


# ------------------------------------------------------------------ the banner


def test_banner_is_the_image_header_of_the_first_picker(chat, settings):
    settings.IVA_WELCOME_BANNER_URL = BANNER
    chat.send("Hi", profile_name="Ravi")
    assert chat.last[0].image_url == BANNER


@pytest.mark.parametrize(
    "url", ["", "http://cdn.example.invalid/b.png", "not a url", "ftp://x/b.png"]
)
def test_missing_or_non_https_banner_gives_a_text_only_picker(chat, settings, url):
    settings.IVA_WELCOME_BANNER_URL = url
    chat.send("Hi", profile_name="Ravi")
    assert chat.keys == ["language.prompt_greeting_named"]
    assert chat.last[0].image_url is None  # same message, no header


def test_banner_is_not_on_a_re_prompt(chat, settings):
    settings.IVA_WELCOME_BANNER_URL = BANNER
    chat.send("Hi")
    chat.send("blah blah")  # not a language: the picker again, plain
    assert chat.keys == ["language.prompt"]
    assert isinstance(chat.last[0], OutButtons)
    assert not isinstance(chat.last[0], OutProductCard)


def test_banner_is_not_on_welcome_back(chat, settings):
    onboard(chat)
    settings.IVA_WELCOME_BANNER_URL = BANNER
    chat.send("Hi")
    assert chat.keys[0] == "greeting"
    assert not any(isinstance(m, OutProductCard) for m in chat.last)
    chat.engine.store._data.clear()  # session expired: "Welcome back"
    chat.send("Hi")
    assert chat.keys[0] == "welcome_back"
    assert not any(isinstance(m, OutProductCard) for m in chat.last)


# ------------------------------------------------------------------ the confirmation


@pytest.mark.parametrize(("lang", "name"), [("en", "English"), ("te", "తెలుగు"), ("hi", "हिन्दी")])
def test_confirmation_is_one_message_in_the_chosen_language(chat, lang, name):
    chat.send("Hi", profile_name="Ravi")
    chat.tap(f"LANGUAGE_SELECT:{lang}")
    assert chat.keys == ["language.set_first"]  # one message: no welcome, no separate menu
    [message] = chat.last
    assert message.body == t(lang, "language.set_first", lang_name=name)
    assert name in message.body
    assert titles(message) == [t(lang, "btn.menu")]
    assert chat.ids == ["CATEGORY_SELECT"]
    assert language_of(chat) == lang
    customer = chat.engine.backend.get_or_create_customer(WA_ID)
    assert customer.onboarding_complete
    assert chat.session().state.name == "MAIN_MENU"


def test_english_confirmation_text(chat):
    chat.send("Hi")
    chat.tap("LANGUAGE_SELECT:en")
    assert chat.body == (
        "Language set to English. You can change it anytime from the menu. What would you like?"
    )


def test_menu_button_after_the_confirmation_opens_the_menu(chat):
    chat.send("Hi")
    chat.tap("LANGUAGE_SELECT:te")
    chat.tap("CATEGORY_SELECT")
    assert chat.keys == ["menu.body"]


def test_typed_language_name_also_confirms_in_one_message(chat):
    chat.send("Hi")
    chat.send("telugu")
    assert chat.keys == ["language.set_first"]
    assert language_of(chat) == "te"


# ---------------------------------------------- THANKS / BYE / ACK, language unset


@pytest.mark.parametrize(
    ("text", "key", "body"),
    [
        ("thanks", "closing.thanks", "You're welcome! 😊 Anything else I can help with?"),
        ("Thank you", "closing.thanks", "You're welcome! 😊 Anything else I can help with?"),
        ("bye", "closing.bye", "Bye! 👋 Message me anytime you'd like to order."),
        ("thanks bye", "closing.bye", "Bye! 👋 Message me anytime you'd like to order."),
    ],
)
def test_thanks_and_bye_while_the_picker_is_showing(chat, text, key, body):
    chat.send("Hi")
    before = chat.session()
    assert before.state.name == "LANGUAGE"
    chat.send(text)
    assert chat.keys == [key]
    assert chat.body == body  # English, no name (none is known)
    assert language_of(chat) is None  # no language was set
    assert chat.session().state.name == "LANGUAGE"  # the picker state is kept
    chat.tap("LANGUAGE_SELECT:hi")  # and the picker still works afterwards
    assert language_of(chat) == "hi"


@pytest.mark.parametrize("text", ["thanks", "bye"])
def test_thanks_or_bye_as_the_very_first_message(chat, text):
    chat.send(text)
    assert chat.keys[0] in ("closing.thanks", "closing.bye")
    assert language_of(chat) is None
    assert chat.session().state.name == "NEW"  # nothing changed
    chat.send("Hi", profile_name="Ravi")  # a later "Hi" still gets the full first-time picker
    assert chat.keys == ["language.prompt_greeting_named"]


@pytest.mark.parametrize("text", ["ok", "cool", "👍", "okay", "theek hai"])
def test_ack_does_not_fire_while_the_language_is_unset(chat, text):
    chat.send(text)  # the very first message
    assert "closing.ack" not in chat.keys
    assert chat.keys == ["language.prompt_greeting_unnamed"]  # the picker, as before
    chat.send(text)  # while the picker is showing
    assert "closing.ack" not in chat.keys
    assert chat.keys == ["language.prompt"]
    assert language_of(chat) is None
    assert chat.session().state.name == "LANGUAGE"


def test_thanks_and_bye_after_the_language_is_set_are_unchanged(chat):
    onboard(chat, "hi", name="Ravi")
    chat.send("thanks")
    assert chat.keys == ["closing.thanks"]
    assert chat.body == t("hi", "closing.thanks_roman", name="Ravi")  # Latin letters: Hinglish

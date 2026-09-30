"""Language: the saved language drives replies; the detected message language never does."""

import pytest

from .harness import PHRASES, WA_ID, in_script, onboard, text_for


@pytest.mark.parametrize(
    ("start", "phrase", "target"),
    [
        ("en", "hindi mein baat karo", "hi"),
        ("en", "తెలుగులో మాట్లాడు", "te"),
        ("te", "switch to english", "en"),
        ("hi", "telugu lo matladu", "te"),
    ],
)
def test_language_change(chat, backend, start, phrase, target):
    onboard(chat, lang=start)
    chat.send(phrase)
    assert chat.keys == ["language.changed", "menu.body"]
    assert chat.body.startswith(text_for(target, "language.changed"))
    assert backend.get_or_create_customer(WA_ID).language == target
    chat.send("show my cart")
    assert chat.body == text_for(target, "cart.empty")


def test_language_change_by_menu(chat, backend):
    onboard(chat)
    chat.tap("LANGUAGE_SELECT")
    assert chat.keys == ["language.prompt"]
    chat.tap("LANGUAGE_SELECT:te")
    assert backend.get_or_create_customer(WA_ID).language == "te"
    assert in_script(chat.message("menu.body").body, "te")


@pytest.mark.parametrize(("saved", "other"), [("en", "te"), ("en", "hi"), ("te", "hi")])
def test_detected_language_never_overrides_the_saved_one(chat, backend, saved, other):
    onboard(chat, lang=saved)
    chat.send(PHRASES[other]["cart"])  # understood, but written in another language
    assert chat.keys == ["cart.empty"]
    assert chat.body == text_for(saved, "cart.empty")
    assert backend.get_or_create_customer(WA_ID).language == saved

"""Transcripts: new customer onboarding, returning customer, diet preference, AI Planner
welcome/menu link. Onboarding is language-only; ``IVA_PLANNER_URL`` is unset by default in
tests (see config/settings/test.py), so ``onboard()`` lands straight on the main menu,
same as before this welcome/planner work. Tests below that need the CTA opt in with the
``settings`` fixture.
"""

import pytest

from apps.conversations.channel import OutCTA
from apps.conversations.renderers import render

from .harness import LANGUAGES, WA_ID, Chat, in_script, onboard

PLANNER_URL = "https://planner.test.invalid/plan"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_new_customer_onboarding(chat, backend, lang):
    chat.send("hi")
    assert chat.keys == ["language.prompt_greeting_unnamed"]  # no profile name in this test
    assert chat.ids == ["LANGUAGE_SELECT:en", "LANGUAGE_SELECT:te", "LANGUAGE_SELECT:hi"]

    chat.tap(f"LANGUAGE_SELECT:{lang}")
    assert chat.keys == ["menu.body"]  # IVA_PLANNER_URL unset in tests: straight to menu

    customer = backend.get_or_create_customer(WA_ID)
    assert customer.onboarding_complete is True
    assert customer.language == lang
    assert customer.name is None
    assert customer.city is None


def test_language_can_be_typed(chat, backend):
    chat.send("hindi")
    assert backend.get_or_create_customer(WA_ID).language == "hi"


def test_unclear_first_message_asks_for_language_again(chat):
    chat.send("hello")
    chat.send("hmm")
    assert chat.keys == ["language.prompt"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_returning_customer_skips_onboarding(engine, store, lang):
    chat = onboard(Chat(engine), lang=lang)
    store.delete(WA_ID)  # new conversation, e.g. days later
    chat.send("hi")
    assert chat.keys == ["welcome_back", "menu.body"]
    assert in_script(chat.message("welcome_back").body.replace("Ravi", ""), lang)


# ------------------------------------------------------------------ AI Planner welcome + CTA


@pytest.mark.parametrize("lang", LANGUAGES)
def test_welcome_cta_when_planner_configured(chat, settings, lang):
    settings.IVA_PLANNER_URL = PLANNER_URL
    chat.send("hi")
    chat.tap(f"LANGUAGE_SELECT:{lang}")
    assert chat.keys == ["welcome.planner_intro", "welcome.browse_prompt"]

    cta = chat.message("welcome.planner_intro")
    assert isinstance(cta, OutCTA)
    assert cta.url == PLANNER_URL
    assert in_script(cta.body, lang)
    assert render(cta) == {
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": cta.body},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": cta.display_text, "url": cta.url},
            },
        },
    }
    browse = chat.message("welcome.browse_prompt")
    assert [b.id for b in browse.buttons] == ["CATEGORY_SELECT"]


def test_first_message_greets_by_profile_name(chat, backend):
    chat.send("hi", profile_name="Ravi Kumar")
    assert chat.keys == ["language.prompt_greeting_named"]
    assert chat.body.startswith("Hi Ravi Kumar! 👋")
    assert backend.get_or_create_customer(WA_ID).name == "Ravi Kumar"


def test_first_message_without_profile_name_has_no_name_greeting(chat, backend):
    chat.send("hi")
    assert chat.keys == ["language.prompt_greeting_unnamed"]
    assert not chat.body.startswith("Hi ")
    assert backend.get_or_create_customer(WA_ID).name is None


def test_profile_name_is_saved_and_not_repeated_in_the_planner_message(chat, backend, settings):
    settings.IVA_PLANNER_URL = PLANNER_URL
    chat.send("hi", profile_name="Ravi Kumar")
    chat.tap("LANGUAGE_SELECT:en")
    assert chat.keys == ["welcome.planner_intro", "welcome.browse_prompt"]
    assert "Ravi Kumar" not in chat.body  # no duplicate greeting after language selection
    assert backend.get_or_create_customer(WA_ID).name == "Ravi Kumar"


def test_missing_profile_name_greets_without_a_name(chat, backend, settings):
    settings.IVA_PLANNER_URL = PLANNER_URL
    chat.send("hi")
    chat.tap("LANGUAGE_SELECT:en")
    assert chat.keys == ["welcome.planner_intro", "welcome.browse_prompt"]
    assert backend.get_or_create_customer(WA_ID).name is None


def test_profile_name_is_sanitized(chat, backend):
    chat.send("hi")
    chat.tap("LANGUAGE_SELECT:en", profile_name="  Ravi\x00  Kumar\n ")
    assert backend.get_or_create_customer(WA_ID).name == "Ravi Kumar"


def test_existing_name_is_never_overwritten(chat, backend):
    onboard(chat, name="Ravi")
    chat.send("show my cart", profile_name="Someone Else")
    assert backend.get_or_create_customer(WA_ID).name == "Ravi"


def test_unset_planner_url_skips_straight_to_main_menu(chat, backend):
    chat.send("hi")
    chat.tap("LANGUAGE_SELECT:en")
    assert chat.keys == ["menu.body"]
    assert backend.get_or_create_customer(WA_ID).onboarding_complete is True


def test_invalid_planner_url_skips_straight_to_main_menu(chat, settings):
    settings.IVA_PLANNER_URL = "http://not-https.example"
    chat.send("hi")
    chat.tap("LANGUAGE_SELECT:en")
    assert chat.keys == ["menu.body"]


def test_planner_row_in_main_menu_when_configured(chat, settings):
    settings.IVA_PLANNER_URL = PLANNER_URL
    onboard(chat)
    chat.send("menu")
    assert any(r.id == "ACT:plan" for r in chat.rows)


def test_planner_row_absent_from_main_menu_when_unset(chat):
    onboard(chat)
    chat.send("menu")
    assert not any(r.id == "ACT:plan" for r in chat.rows)


def test_tapping_plan_my_meals_resends_the_cta(chat, settings):
    settings.IVA_PLANNER_URL = PLANNER_URL
    onboard(chat)
    chat.tap("ACT:plan")
    assert chat.keys == ["planner.cta_body"]
    assert isinstance(chat.last[0], OutCTA)


def test_plan_action_falls_back_to_menu_once_url_becomes_unset(chat, settings):
    settings.IVA_PLANNER_URL = PLANNER_URL
    onboard(chat)
    settings.IVA_PLANNER_URL = ""
    chat.tap("ACT:plan")
    assert chat.keys == ["menu.body"]


# ------------------------------------------------------------------ diet (change preference)


def test_diet_can_be_changed_later(chat, backend):
    onboard(chat, diet="skip")
    chat.tap("ACT:diet")
    assert chat.keys == ["diet.ask"]
    chat.tap("ACT:diet:egg")
    assert chat.keys == ["diet.saved", "menu.body"]
    assert backend.get_or_create_customer(WA_ID).diet == "egg"


def test_diet_can_be_typed_when_changing_later(chat, backend):
    onboard(chat, diet="skip")
    chat.tap("ACT:diet")
    chat.send("non veg")
    assert backend.get_or_create_customer(WA_ID).diet == "non_veg"


def test_unrecognized_free_text_while_changing_diet_falls_back(chat):
    # Unlike the old onboarding-time diet step, changing the preference later has no
    # dedicated "try again" reprompt for unrecognized text - it just falls through to
    # the normal intent pipeline, same as any other unrecognized message would.
    onboard(chat, diet="skip")
    chat.tap("ACT:diet")
    chat.send("maybe")
    assert chat.keys == ["fallback"]


def test_skip_diet(chat, backend):
    onboard(chat, diet="skip")
    customer = backend.get_or_create_customer(WA_ID)
    assert customer.diet is None
    assert customer.onboarding_complete is True


def test_skip_when_changing_later_keeps_the_preference(chat, backend):
    onboard(chat, diet="veg")
    chat.tap("ACT:diet").tap("ACT:diet:skip")
    assert chat.keys == ["menu.body"]
    assert backend.get_or_create_customer(WA_ID).diet == "veg"

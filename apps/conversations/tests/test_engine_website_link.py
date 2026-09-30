"""Transcripts: the "See full menu on website" main-menu option (IVA_WEBSITE_SHOP_URL)."""

from dataclasses import replace

import pytest

from apps.conversations.channel import MAX_ROWS, OutCTA
from apps.conversations.renderers import BUTTON_TITLE_MAX, ROW_TITLE_MAX, render

from .harness import LANGUAGES, in_script, onboard

SHOP_URL = "https://shop.test.invalid/shop"
PLANNER_URL = "https://planner.test.invalid/plan"
ROW_ID = "ACT:website"


def menu_ids(chat) -> list[str]:
    return [r.id for r in chat.rows]


def test_option_shown_when_configured(chat, settings):
    settings.IVA_WEBSITE_SHOP_URL = SHOP_URL
    onboard(chat)
    chat.send("menu")
    assert ROW_ID in menu_ids(chat)
    row = next(r for r in chat.rows if r.id == ROW_ID)
    assert row.title == "See full menu on website"


def test_option_hidden_when_unset(chat):
    onboard(chat)
    chat.send("menu")
    assert ROW_ID not in menu_ids(chat)


@pytest.mark.parametrize(
    "bad", ["http://insecure.example/shop", "not a url", "https://", "ftp://x.y"]
)
def test_option_hidden_when_invalid_or_not_https(chat, settings, bad):
    settings.IVA_WEBSITE_SHOP_URL = bad
    onboard(chat)
    chat.send("menu")
    assert ROW_ID not in menu_ids(chat)


def test_tap_sends_a_valid_cta_url_message(chat, settings):
    settings.IVA_WEBSITE_SHOP_URL = SHOP_URL
    onboard(chat)
    chat.tap(ROW_ID)
    assert chat.keys == ["website.cta_body"]
    cta = chat.last[0]
    assert isinstance(cta, OutCTA)
    assert cta.url == SHOP_URL
    payload = render(cta)["interactive"]
    assert payload["type"] == "cta_url"
    assert payload["action"] == {
        "name": "cta_url",
        "parameters": {"display_text": "Open full menu", "url": SHOP_URL},
    }
    assert len(payload["action"]["parameters"]["display_text"]) <= BUTTON_TITLE_MAX


@pytest.mark.parametrize("lang", LANGUAGES)
def test_cta_and_row_are_localized_and_fit_limits(chat, settings, lang):
    settings.IVA_WEBSITE_SHOP_URL = SHOP_URL
    onboard(chat, lang=lang)
    chat.send("menu")
    row = next(r for r in chat.rows if r.id == ROW_ID)
    assert in_script(row.title, lang)
    assert len(row.title) <= ROW_TITLE_MAX
    chat.tap(ROW_ID)
    assert in_script(chat.body, lang)
    assert len(chat.last[0].display_text) <= BUTTON_TITLE_MAX


def test_tap_falls_back_to_the_menu_once_the_url_is_unset(chat, settings):
    settings.IVA_WEBSITE_SHOP_URL = SHOP_URL
    onboard(chat)
    settings.IVA_WEBSITE_SHOP_URL = ""
    chat.tap(ROW_ID)
    assert chat.keys == ["menu.body"]


@pytest.mark.parametrize(
    ("shop", "planner"),
    [("", ""), (SHOP_URL, ""), ("", PLANNER_URL), (SHOP_URL, PLANNER_URL)],
)
def test_menu_row_count_within_whatsapp_limit(chat, backend, settings, shop, planner):
    settings.IVA_WEBSITE_SHOP_URL = shop
    settings.IVA_PLANNER_URL = planner
    onboard(chat)
    chat.send("menu")
    assert 1 <= len(chat.rows) <= MAX_ROWS
    assert len({r.id for r in chat.rows}) == len(chat.rows)


def test_menu_still_fits_when_the_backend_has_many_groups(chat, backend, settings):
    settings.IVA_WEBSITE_SHOP_URL = SHOP_URL
    settings.IVA_PLANNER_URL = PLANNER_URL
    real = backend.list_categories
    base = real(None)[0]

    def list_categories(parent_id=None):
        if parent_id is None:
            return [replace(base, id=f"g{i}", name=f"Group {i}") for i in range(8)]
        return real(parent_id)

    backend.list_categories = list_categories
    onboard(chat)
    chat.send("menu")
    ids = menu_ids(chat)
    assert len(ids) == MAX_ROWS
    assert ROW_ID in ids
    assert "ACT:plan" in ids
    assert sum(i.startswith("CATEGORY_SELECT:") for i in ids) == 3  # groups yield to the links

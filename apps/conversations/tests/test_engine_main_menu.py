"""The main menu: sections, row descriptions, the live cart line, WhatsApp limits and the
rollback switch. Row IDs and what they do are unchanged (snapshotted below)."""

import time
from decimal import Decimal

import pytest

from apps.conversations.backend_port import BackendUnavailable
from apps.conversations.channel import MAX_ROWS, OutList
from apps.conversations.messages import CATALOGS, t
from apps.conversations.renderers import (
    INTERACTIVE_BODY_MAX,
    LIST_BUTTON_MAX,
    ROW_DESCRIPTION_MAX,
    ROW_TITLE_MAX,
    SECTION_TITLE_MAX,
    render,
)
from apps.iva_common.money import format_money

from .harness import LANGUAGES, onboard

PLANNER = "https://planner.test.invalid/plan"
WEBSITE = "https://shop.test.invalid"

# The row IDs the menu had BEFORE this change, per configuration (snapshot).
GROUP_IDS = ["CATEGORY_SELECT:fresh", "CATEGORY_SELECT:packaged"]
ALWAYS_IDS = ["VIEW_CART", "ACT:orders", "HELP", "LANGUAGE_SELECT", "ACT:diet"]
OLD_FLAT_ORDER_BOTH = [*GROUP_IDS, *ALWAYS_IDS, "ACT:plan", "ACT:website"]


def menu_of(chat) -> OutList:
    return next(m for m in chat.last if isinstance(m, OutList) and m.key == "menu.body")


def open_menu(chat, lang="en", **settings_values):
    onboard(chat, lang)
    chat.tap("CATEGORY_SELECT")
    return menu_of(chat)


@pytest.fixture
def links(settings):
    settings.IVA_PLANNER_URL = PLANNER
    settings.IVA_WEBSITE_SHOP_URL = WEBSITE


# ------------------------------------------------------------------ one builder


def test_every_path_sends_the_same_menu(chat, links):
    """Menu button, greeting, welcome back and the fallback all use the one builder."""
    onboard(chat)
    chat.tap("CATEGORY_SELECT")
    expected = menu_of(chat)
    chat.send("hi")
    assert menu_of(chat) == expected
    chat.engine.store._data.clear()  # expired session: welcome back
    chat.send("hi")
    assert chat.keys[0] == "welcome_back"
    assert menu_of(chat) == expected


# ------------------------------------------------------------------ ids, sections, order


def test_row_ids_are_exactly_the_old_set_with_both_links(chat, links):
    menu = open_menu(chat)
    ids = [r.id for r in menu.rows]
    assert sorted(ids) == sorted(OLD_FLAT_ORDER_BOTH)
    assert len(ids) == len(set(ids)) and len(ids) <= MAX_ROWS


def test_row_ids_without_links_match_the_old_set(chat):
    menu = open_menu(chat)
    assert sorted(r.id for r in menu.rows) == sorted([*GROUP_IDS, *ALWAYS_IDS])


def test_only_the_planner_link(chat, settings):
    settings.IVA_PLANNER_URL = PLANNER
    menu = open_menu(chat)
    assert sorted(r.id for r in menu.rows) == sorted([*GROUP_IDS, *ALWAYS_IDS, "ACT:plan"])


def test_only_the_website_link(chat, settings):
    settings.IVA_WEBSITE_SHOP_URL = WEBSITE
    menu = open_menu(chat)
    assert sorted(r.id for r in menu.rows) == sorted([*GROUP_IDS, *ALWAYS_IDS, "ACT:website"])


def test_sections_and_order(chat, links):
    menu = open_menu(chat)
    assert [(title, [r.id for r in rows]) for title, rows in menu.sections] == [
        ("Order", [*GROUP_IDS, "ACT:plan"]),
        ("Your orders", ["VIEW_CART", "ACT:orders"]),
        ("More", ["ACT:diet", "LANGUAGE_SELECT", "HELP", "ACT:website"]),
    ]
    assert [r.id for r in menu.rows] == [r.id for _, rows in menu.sections for r in rows]


def test_a_hidden_row_leaves_its_section_without_it_never_empty(chat):
    menu = open_menu(chat)  # no planner, no website
    assert [(title, [r.id for r in rows]) for title, rows in menu.sections] == [
        ("Order", GROUP_IDS),
        ("Your orders", ["VIEW_CART", "ACT:orders"]),
        ("More", ["ACT:diet", "LANGUAGE_SELECT", "HELP"]),
    ]
    assert all(rows for _, rows in menu.sections)


def test_an_empty_section_is_never_sent(chat, monkeypatch):
    onboard(chat)
    monkeypatch.setattr(chat.engine.backend, "list_categories", lambda parent: [])
    chat.tap("CATEGORY_SELECT")
    menu = menu_of(chat)
    assert [title for title, _ in menu.sections] == ["Your orders", "More"]  # no "Order" at all
    assert all(title for title, _ in menu.sections)


def test_row_titles_are_unchanged_in_every_language(chat, links):
    for lang in LANGUAGES:
        chat.engine.store._data.clear()
        menu = open_menu(chat, lang)
        titles = {r.id: r.title for r in menu.rows}
        assert titles["VIEW_CART"] == t(lang, "menu.cart")
        assert titles["ACT:orders"] == t(lang, "menu.orders")
        assert titles["HELP"] == t(lang, "menu.help")
        assert titles["LANGUAGE_SELECT"] == t(lang, "menu.language")
        assert titles["ACT:diet"] == t(lang, "menu.diet")
        assert titles["ACT:plan"] == t(lang, "btn.planner")
        assert titles["ACT:website"] == t(lang, "menu.website")


# ------------------------------------------------------------------ descriptions


def test_descriptions_in_english(chat, links):
    menu = open_menu(chat)
    desc = {r.id: r.description for r in menu.rows}
    assert desc["CATEGORY_SELECT:fresh"] == "Bowls, café favourites, wellness shots"
    assert desc["CATEGORY_SELECT:packaged"] == "Teas, energy bites, protein and more"
    assert desc["ACT:plan"] == "Get a plan from our AI planner"
    assert desc["ACT:orders"] == "Past orders, payment status, reorder"
    for plain in ("ACT:diet", "LANGUAGE_SELECT", "HELP", "ACT:website"):
        assert desc[plain] is None  # rows in "More" have no description


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_descriptions_exist_in_hindi_and_telugu(chat, links, lang):
    menu = open_menu(chat, lang)
    desc = {r.id: r.description for r in menu.rows}
    for row_id, key in (
        ("CATEGORY_SELECT:fresh", "menu.desc.fresh"),
        ("CATEGORY_SELECT:packaged", "menu.desc.packaged"),
        ("ACT:plan", "menu.desc.plan"),
        ("ACT:orders", "menu.desc.orders"),
    ):
        assert desc[row_id] == t(lang, key)
    assert [title for title, _ in menu.sections] == [
        t(lang, "menu.section.order"),
        t(lang, "menu.section.yours"),
        t(lang, "menu.section.more"),
    ]


def test_descriptions_make_no_promise_about_tracking_or_delivery_time():
    for lang, catalog in CATALOGS.items():
        for key, text in catalog.items():
            if key.startswith("menu.desc"):
                for word in ("track", "deliver", "eta", "minutes", "ट्रैक", "डिलीवरी", "ట్రాక్"):
                    assert word not in text.lower(), (lang, key)


def test_the_category_descriptions_match_the_catalog(chat):
    """Every word promised in a group description names a real category of that group."""
    backend = chat.engine.backend
    names = {}
    for group in backend.list_categories(None):
        names[group.id] = " ".join(c.name.lower() for c in backend.list_categories(group.id))
    for word in ("bowls", "café", "wellness"):
        assert word in names["fresh"]
    for word in ("teas", "energy bites", "protein"):
        assert word in names["packaged"]


# ------------------------------------------------------------------ the live cart line


def cart_desc(chat, lang="en"):
    chat.tap("CATEGORY_SELECT")
    return {r.id: r.description for r in menu_of(chat).rows}["VIEW_CART"]


def test_cart_line_when_empty(chat):
    onboard(chat)
    assert cart_desc(chat) == "Cart is empty"


def test_cart_line_with_one_item(chat):
    onboard(chat)
    chat.send("add 1 makhana")
    cart = chat.engine.backend.get_cart(
        chat.engine.backend.get_or_create_customer("919876500001").id
    )
    assert sum(line.quantity for line in cart.lines) == 1
    assert cart_desc(chat) == f"1 item · {format_money(cart.subtotal, cart.currency)}"


def test_cart_line_with_several_items(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("add 3 makhana")
    cart = chat.engine.backend.get_cart(
        chat.engine.backend.get_or_create_customer("919876500001").id
    )
    assert sum(line.quantity for line in cart.lines) == 5
    assert cart_desc(chat) == f"5 items · {format_money(cart.subtotal, cart.currency)}"
    assert "₹" in cart_desc(chat)


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_cart_line_in_hindi_and_telugu(chat, lang):
    onboard(chat, lang)
    assert cart_desc(chat) == t(lang, "menu.cart_desc.empty")
    chat.send({"hi": "दो पैकेट मखाना जोड़ो", "te": "రెండు ప్యాకెట్లు మఖానా జోడించు"}[lang])
    line = cart_desc(chat)
    assert "₹" in line and line != t(lang, "menu.cart_desc.empty")


def test_cart_line_falls_back_to_static_text_when_the_lookup_fails(chat, flaky):
    onboard(chat)
    chat.send("add 2 makhana")
    flaky.fail["get_cart"] = BackendUnavailable("down")
    assert cart_desc(chat) == "See what's in your cart"
    assert chat.keys == ["menu.body"]  # the menu was sent


def test_cart_line_falls_back_to_static_text_when_the_lookup_is_slow(
    chat, backend, settings, monkeypatch
):
    onboard(chat)
    chat.send("add 2 makhana")
    settings.IVA_MENU_CART_TIMEOUT_SECONDS = 0.05
    real_get_cart = backend.get_cart

    def slow_get_cart(*args, **kwargs):
        time.sleep(0.4)
        return real_get_cart(*args, **kwargs)

    monkeypatch.setattr(backend, "get_cart", slow_get_cart)
    started = time.monotonic()
    line = cart_desc(chat)
    assert line == "See what's in your cart"
    assert time.monotonic() - started < 0.35  # the menu did not wait for the Port
    assert chat.keys == ["menu.body"]


# ------------------------------------------------------------------ WhatsApp limits

LIMITS = {
    "section title": SECTION_TITLE_MAX,
    "row title": ROW_TITLE_MAX,
    "row description": ROW_DESCRIPTION_MAX,
    "button label": LIST_BUTTON_MAX,
    "body": INTERACTIVE_BODY_MAX,
}


def worst_case_cart_lines(lang):
    subtotal = format_money(Decimal("9999999.99"), "INR")  # ₹99,99,999.99
    return [
        t(lang, "menu.cart_desc.many", n=99, subtotal=subtotal),
        t(lang, "menu.cart_desc.one", subtotal=subtotal),
    ]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_menu_string_fits_whatsapp_limits(lang):
    catalog = CATALOGS[lang]
    for key in ("menu.section.order", "menu.section.yours", "menu.section.more"):
        assert len(catalog[key]) <= LIMITS["section title"] - 2, (lang, key)  # 2 spare
    row_titles = [
        "menu.cart", "menu.orders", "menu.help", "menu.language", "menu.diet",
        "menu.website", "btn.planner",
    ]  # fmt: skip
    for key in row_titles:  # existing titles are unchanged; the hard limit applies
        assert len(catalog[key]) <= LIMITS["row title"], (lang, key)
    descriptions = [
        "menu.desc.fresh", "menu.desc.packaged", "menu.desc.plan", "menu.desc.orders",
        "menu.cart_desc.empty", "menu.cart_desc.static",
    ]  # fmt: skip
    for key in descriptions:
        assert len(catalog[key]) <= LIMITS["row description"], (lang, key)
    for line in worst_case_cart_lines(lang):
        assert len(line) <= LIMITS["row description"], (lang, line)
    assert len(catalog["menu.button"]) <= LIMITS["button label"]
    assert len(catalog["menu.body"]) <= LIMITS["body"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_rendered_menu_is_not_truncated_and_has_at_most_ten_rows(chat, links, lang):
    open_menu(chat, lang)
    menu = menu_of(chat)
    payload = render(menu)["interactive"]
    sections = payload["action"]["sections"]
    assert 1 < len(sections) and all(s["title"] for s in sections)  # titled when several
    rendered_rows = [row for s in sections for row in s["rows"]]
    assert len(rendered_rows) <= MAX_ROWS
    for source, sent in zip(menu.rows, rendered_rows, strict=True):
        assert sent["title"] == source.title  # nothing was cut with "…"
        assert sent.get("description") == source.description
    for (title, _), s in zip(menu.sections, sections, strict=True):
        assert s["title"] == title
    assert len(payload["action"]["button"]) <= LIST_BUTTON_MAX


# ------------------------------------------------------------------ every row still routes


ROUTES = [
    ("CATEGORY_SELECT:fresh", "group.body"),
    ("CATEGORY_SELECT:packaged", "group.body"),
    ("VIEW_CART", "cart.empty"),
    ("HELP", "help"),
    ("LANGUAGE_SELECT", "language.prompt"),
    ("ACT:diet", "diet.ask"),
    ("ACT:plan", "planner.cta_body"),
    ("ACT:website", "website.cta_body"),
]


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize(("row_id", "key"), ROUTES)
def test_each_row_routes_to_the_same_handler(chat, links, lang, row_id, key):
    menu = open_menu(chat, lang)
    assert row_id in [r.id for r in menu.rows]
    chat.tap(row_id)
    assert chat.keys[0] == key


@pytest.mark.parametrize("lang", LANGUAGES)
def test_my_orders_row_still_opens_order_history(chat, links, lang):
    menu = open_menu(chat, lang)
    assert "ACT:orders" in [r.id for r in menu.rows]
    chat.tap("ACT:orders")
    assert chat.keys and not chat.keys[0].startswith("menu")
    assert chat.keys[0] != "fallback"


# ------------------------------------------------------------------ the rollback switch


def test_flag_false_restores_the_old_flat_list(chat, links, settings):
    settings.IVA_MENU_SECTIONS_ENABLED = False
    menu = open_menu(chat)
    assert menu.sections == ()
    assert [r.id for r in menu.rows] == OLD_FLAT_ORDER_BOTH  # the old order, exactly
    assert all(r.description is None for r in menu.rows)  # no descriptions
    assert [r.title for r in menu.rows if r.id in ALWAYS_IDS] == [
        "View cart", "My orders", "Help", "Change language", "Food preference"
    ]  # fmt: skip
    payload = render(menu)["interactive"]["action"]
    assert len(payload["sections"]) == 1 and "title" not in payload["sections"][0]


def test_flag_false_never_looks_up_the_cart(chat, flaky, settings):
    onboard(chat)
    settings.IVA_MENU_SECTIONS_ENABLED = False
    flaky.fail["get_cart"] = BackendUnavailable("down")
    chat.tap("CATEGORY_SELECT")
    assert chat.keys == ["menu.body"]

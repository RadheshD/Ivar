"""Transcripts: menu, both groups, categories, pagination, search, ambiguity, diet filter."""

import pytest

from apps.conversations.engine import CARD_PAGE_SIZE

from .harness import LANGUAGES, PHRASES, WA_ID, in_script, onboard


def card_names(chat) -> set[str]:
    return {c.body.splitlines()[0] for c in chat.cards}


def row_names(chat) -> set[str]:
    return {r.title for r in chat.rows if r.id.startswith("ACT:showcard:")}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_browse_both_groups(chat, lang):
    onboard(chat, lang=lang)
    menu = chat.ids
    assert menu[:2] == ["CATEGORY_SELECT:fresh", "CATEGORY_SELECT:packaged"]

    chat.tap("CATEGORY_SELECT:fresh")
    assert chat.keys == ["group.body"]
    assert [r.title for r in chat.rows] == [
        "Café Favourites",
        "Protein Bowls",
        "Non-Veg Snacks",
        "Wellness Shots",
    ]
    chat.tap("CATEGORY_SELECT:cafe-favourites")  # 7 products: cards, 4 per page
    assert chat.keys[0] == "category.body"
    assert len(chat.cards) == CARD_PAGE_SIZE
    assert all(
        b.id.startswith(("ACT:addcard:", "ACT:ordernow:")) for c in chat.cards for b in c.buttons
    )
    assert "₹" in chat.cards[0].body  # price from the port
    assert "ACT:more" in chat.ids  # 3 more beyond this page

    chat.tap("CATEGORY_SELECT:packaged")
    assert chat.keys == ["group.body"]
    assert len(chat.rows) == 7
    assert in_script(chat.body.replace("Packaged Foods", ""), lang)


def test_pagination_beyond_four(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")  # 10 products
    assert len(chat.cards) == CARD_PAGE_SIZE
    assert chat.ids[-2:] == ["ACT:more", "CATEGORY_SELECT"]
    assert "1–4 of 10" in chat.body
    first_page = card_names(chat)

    chat.tap("ACT:more")
    assert len(chat.cards) == CARD_PAGE_SIZE
    assert "5–8 of 10" in chat.body
    second_page = card_names(chat)
    assert first_page.isdisjoint(second_page)

    chat.tap("ACT:more")
    assert len(chat.cards) == 2
    assert "9–10 of 10" in chat.body
    assert chat.ids[-1] == "CATEGORY_SELECT"  # last page: no "Show more"
    assert card_names(chat).isdisjoint(first_page | second_page)


def test_menu_intent_without_category_is_the_main_menu(chat):
    onboard(chat)
    chat.send("menu")
    assert chat.keys == ["menu.body"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_search_by_typing(chat, lang):
    onboard(chat, lang=lang)
    chat.send(PHRASES[lang]["search"])
    assert chat.keys == ["product.card"]  # a single match is the same card as browsing
    assert chat.cards[0].body.startswith("Makhana Crunch")
    assert "₹199" in chat.body
    assert len(chat.cards[0].buttons) == 2


def test_typo_search(chat):
    onboard(chat)
    chat.send("makana")
    assert chat.cards[0].body.startswith("Makhana Crunch")


def test_ambiguous_product_offers_a_results_list_then_a_card(chat, backend):
    onboard(chat)
    chat.send("oats")
    assert chat.keys == ["choose.product"]
    assert row_names(chat) == {"Classic Oats Bowl", "Ivar Oats", "Oats & Millet Cookies"}
    assert not chat.cards

    row = next(r for r in chat.rows if r.title == "Ivar Oats")
    chat.tap(row.id)
    assert len(chat.cards) == 1
    card = chat.cards[0]
    add_id = next(b.id for b in card.buttons if b.title == "Add to cart")
    chat.tap(add_id)
    assert chat.keys[0] == "cart.added"
    assert "Ivar Oats" in chat.body


def test_choice_by_position(chat):
    onboard(chat)
    chat.send("oats")
    second = chat.rows[1].title
    chat.send("the second one")
    assert chat.message("product.detail").body.startswith(second)


def test_no_match_says_so_and_offers_the_menu(chat):
    onboard(chat)
    chat.send("do you have pizza")
    assert chat.keys == ["search.none"]
    assert chat.ids == ["CATEGORY_SELECT"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_ambiguous_category(chat, lang):
    onboard(chat, lang=lang)
    chat.send(PHRASES[lang]["snacks"])
    assert chat.keys == ["category.choose"]
    assert chat.ids == ["CATEGORY_SELECT:non-veg-snacks", "CATEGORY_SELECT:nuts-seeds-snacks"]


def test_single_category_by_name(chat):
    onboard(chat)
    chat.send("show me breakfast")  # 4 products: fits on one page, no "Show more"
    assert chat.keys[0] == "category.body"
    assert "Breakfast & Grains" in chat.body
    assert len(chat.cards) == 4
    assert "ACT:more" not in chat.ids


def test_group_by_name(chat):
    onboard(chat)
    chat.send("fresh")
    assert chat.keys == ["group.body"]


def test_unknown_category_reference_is_not_found(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:no-such-category")
    assert chat.keys == ["error.not_found"]


@pytest.mark.parametrize(
    ("diet", "visible", "keys"),
    [
        ("veg", 0, ["diet.hidden_all"]),
        ("egg", 1, ["category.body", "diet.hidden_some"]),
        ("non_veg", 5, ["category.body"]),
    ],
)
def test_diet_filter_is_passed_to_the_port_and_never_silent(chat, diet, visible, keys):
    onboard(chat, diet=diet)
    chat.tap("CATEGORY_SELECT:non-veg-snacks")  # 5 products: 4 non-veg, 1 egg
    assert chat.keys[0] == keys[0]
    assert len(chat.cards) == min(visible, CARD_PAGE_SIZE)
    if visible:
        assert f"of {visible}" in chat.body
    if "diet.hidden_some" in keys:
        assert "4" in chat.message("diet.hidden_some").body


DIET_ACTIONS = ["ACT:showall", "ACT:diet", "CATEGORY_SELECT"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_search_hidden_by_diet_says_so(chat, backend, lang):
    onboard(chat, lang=lang, diet="veg")
    chat.send("do you have chicken")
    assert chat.keys == ["diet.hidden_all"]
    assert chat.ids == DIET_ACTIONS
    assert in_script(chat.body.replace("chicken", ""), lang)
    assert backend.get_or_create_customer(WA_ID).diet == "veg"  # nothing changed


def test_show_anyway_lists_the_hidden_matches(chat, backend):
    onboard(chat, diet="veg")
    chat.send("do you have chicken")
    chat.tap("ACT:showall")
    assert chat.keys[0] == "search.results"
    assert row_names(chat) == {
        "Chicken Pepper Bites",
        "Chicken Seekh Kebab Bites",
        "Grilled Chicken Quinoa Bowl",
    }
    assert backend.get_or_create_customer(WA_ID).diet == "veg"  # preference kept


def test_show_anyway_for_a_category(chat):
    onboard(chat, diet="veg")
    chat.tap("CATEGORY_SELECT:non-veg-snacks")
    chat.tap("ACT:showall")
    assert chat.keys[0] == "category.body"
    assert len(chat.cards) == min(5, CARD_PAGE_SIZE)  # 4 of 5 on the first page


def test_change_preference_from_the_notice(chat, backend):
    onboard(chat, diet="veg")
    chat.send("do you have chicken")
    chat.tap("ACT:diet")
    assert chat.keys == ["diet.ask"]
    chat.tap("ACT:diet:non_veg")
    assert backend.get_or_create_customer(WA_ID).diet == "non_veg"
    chat.send("do you have chicken")
    assert chat.keys[0] == "choose.product"


def test_partly_hidden_search_shows_matches_and_a_notice(chat):
    onboard(chat, diet="veg")
    chat.send("do you have crunch")  # Makhana, Jowar (veg) and Egg Masala Crunch (egg)
    assert chat.keys[0] == "choose.product"
    assert chat.keys[-1] == "diet.hidden_some"
    assert row_names(chat) == {"Makhana Crunch", "Jowar Crunch"}
    assert chat.ids[-3:] == DIET_ACTIONS


def test_no_notice_when_nothing_is_hidden(chat):
    onboard(chat, diet="veg")
    chat.send("oats")
    assert chat.keys[0] == "choose.product"
    assert "diet.hidden_some" not in chat.keys


def test_out_of_stock_product(chat, backend):
    onboard(chat)
    backend.set_stock("makhana-crunch", False)
    chat.send("do you have makhana")
    assert chat.keys == ["product.unavailable"]  # re-checked through the port; no card
    assert not any(i.startswith("ADD_TO_CART") for i in chat.ids)


def test_catalog_names_are_passed_to_the_pipeline_as_hints(chat):
    onboard(chat)
    chat.send("roasted chana mix kavali")  # exact product name, matched via the hint
    assert chat.cards[0].body.startswith("Roasted Chana Mix")

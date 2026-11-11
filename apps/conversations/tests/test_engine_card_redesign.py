"""Transcripts: redesigned product cards -- tags, quantity step, confirmation, footer."""

import logging

import pytest

from apps.conversations.card_content import CARD_BODY_MAX
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.tests.harness import (
    Chat,
    onboard,
    resize_category,
    tap_card_quantity,
    write_fixture,
)

BASE = "https://cdn.example.test/assets/products/"
NUTS = "CATEGORY_SELECT:nuts-seeds-snacks"  # 5 products in the fixture
CAFE = "CATEGORY_SELECT:cafe-favourites"


def make_chat(make_engine, tmp_path, mutate=None, *, base=BASE) -> Chat:
    path = write_fixture(tmp_path, mutate or (lambda data: None))
    return Chat(make_engine(FakeBackend(path, image_base_url=base)))


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(chat.whatsapp_id)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


def card_named(chat, name):
    return next(c for c in chat.carousel.cards if c.body.startswith(name))


def button_id(card, title):
    return next(b.id for b in card.buttons if b.title == title)


def start_add(chat, name="Seed Mix"):
    chat.tap(NUTS)
    chat.tap(card_named(chat, name).buttons[0].id)  # the first button is "Add to cart"


# ------------------------------------------------------------------ card content


def test_card_body_is_name_price_then_diet_and_protein_tags(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    chat.tap(NUTS)
    assert card_named(chat, "Seed Mix").body == "Seed Mix\n₹349\nVeg · Protein: 6 g"


def test_a_missing_protein_leaves_that_tag_out(make_engine, tmp_path):
    def drop(data):
        next(p for p in data["products"] if p["id"] == "seed-mix")["protein_g"] = None

    chat = make_chat(make_engine, tmp_path, drop)
    onboard(chat)
    chat.tap(NUTS)
    assert card_named(chat, "Seed Mix").body == "Seed Mix\n₹349\nVeg"


def test_every_card_body_fits_meta_limits_even_for_a_very_long_name(make_engine, tmp_path):
    def rename(data):
        next(p for p in data["products"] if p["id"] == "seed-mix")["name"] = "Seed Mix " * 40

    chat = make_chat(make_engine, tmp_path, rename)
    onboard(chat)
    chat.tap(NUTS)
    for card in chat.carousel.cards:
        assert len(card.body) <= CARD_BODY_MAX and card.body.count("\n") <= 2
    long = next(c for c in chat.carousel.cards if c.body.startswith("Seed Mix Seed Mix"))
    assert long.body.split("\n")[0].endswith("…") and long.body.endswith("Veg · Protein: 6 g")


def test_no_benefit_line_is_added(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    chat.tap(NUTS)
    assert all(card.body.count("\n") <= 2 for card in chat.carousel.cards)
    assert "healthy" not in " ".join(card.body.lower() for card in chat.carousel.cards)


# ------------------------------------------------------------------ photos


def test_a_product_without_a_photo_falls_back_to_cards_and_is_logged(make_engine, tmp_path, caplog):
    def drop(data):
        next(p for p in data["products"] if p["id"] == "seed-mix")["image_path"] = None

    chat = make_chat(make_engine, tmp_path, drop)
    onboard(chat)
    with caplog.at_level(logging.WARNING, logger="apps.conversations.engine"):
        chat.tap(NUTS)
    assert chat.carousel is None and len(chat.cards) == 4  # no broken carousel, no crash
    seed = next(c for c in chat.cards if c.body.startswith("Seed Mix"))
    assert seed.image_url is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "without a usable photo" in logged and "seed-mix" in logged


# ------------------------------------------------------------------ footer ("last card")


def test_footer_offers_see_more_and_back_to_menu_when_more_remain(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path, lambda d: resize_category(d, "cafe-favourites", 12))
    onboard(chat)
    chat.tap(CAFE)
    footer = chat.last[-1]
    assert [b.title for b in footer.buttons] == ["See more", "Back to menu"]
    assert footer.buttons[0].id == "ACT:more"


def test_footer_is_only_back_to_menu_when_nothing_remains(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    chat.tap(NUTS)
    assert [b.title for b in chat.last[-1].buttons] == ["Back to menu"]


def test_see_more_shows_the_next_page_of_the_same_category(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path, lambda d: resize_category(d, "cafe-favourites", 12))
    onboard(chat)
    chat.tap(CAFE)
    first = {c.body.split("\n")[0] for c in chat.carousel.cards}
    chat.tap("ACT:more")
    second = {c.body.split("\n")[0] for c in chat.last[0].cards} if chat.carousel is None else None
    page = chat.carousel.cards if chat.carousel else chat.cards
    names = {c.body.split("\n")[0] for c in page}
    assert names and not names & first
    assert second is None


# ------------------------------------------------------------------ quantity step


def test_add_to_cart_asks_for_a_quantity_with_buttons_1_2_3_and_adds_nothing(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    assert chat.keys == ["card.ask_qty"]
    assert [b.title for b in chat.last[0].buttons] == ["1", "2", "3"]
    assert cart_of(chat) == {}


@pytest.mark.parametrize("quantity", [1, 2, 3])
def test_tapping_a_quantity_adds_that_many_and_confirms_with_the_cart_total(
    make_engine, tmp_path, quantity
):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    tap_card_quantity(chat, quantity)
    assert chat.keys == ["card.added"]
    assert cart_of(chat) == {"Seed Mix": quantity}
    total = f"₹{349 * quantity:,}"
    assert (
        chat.body.startswith(f"Added {quantity} × Seed Mix") and f"Cart total: {total}" in chat.body
    )
    assert [b.title for b in chat.last[0].buttons] == ["Checkout", "Add more", "Edit cart"]


def test_a_typed_number_is_accepted(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    chat.send("5")
    assert chat.keys == ["card.added"] and cart_of(chat) == {"Seed Mix": 5}


@pytest.mark.parametrize("typed", ["0", "21", "99999", "9" * 60, "1000000000000"])
def test_invalid_or_huge_input_asks_again_and_keeps_waiting(make_engine, tmp_path, typed):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    chat.send(typed)
    assert chat.keys == ["card.qty_range"] and cart_of(chat) == {}
    chat.send("2")  # still waiting for the quantity of the same product
    assert chat.keys == ["card.added"] and cart_of(chat) == {"Seed Mix": 2}


def test_text_that_is_not_a_number_asks_again(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    chat.send("blah blah")
    assert chat.keys == ["qty.ask"] and cart_of(chat) == {}
    chat.send("3")
    assert cart_of(chat) == {"Seed Mix": 3}


def test_an_old_quantity_prompt_adds_its_own_product(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    chat.tap(NUTS)
    add_seed = button_id(card_named(chat, "Seed Mix"), "Add to cart")
    add_makhana = button_id(card_named(chat, "Makhana Crunch"), "Add to cart")
    chat.tap(add_seed)
    old = next(i for i in chat.ids if i.endswith(":2"))
    chat.tap(add_makhana)  # a newer prompt is now the one waiting
    chat.tap(old)  # the Seed Mix prompt, tapped late
    assert cart_of(chat) == {"Seed Mix": 2}


def test_quantity_for_a_product_that_went_out_of_stock_is_handled(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    chat.engine.backend.set_stock("seed-mix", False)
    tap_card_quantity(chat, 1)
    assert chat.keys[0] == "cards.stale" and cart_of(chat) == {}


# ------------------------------------------------------------------ confirmation buttons


def test_add_more_returns_to_the_same_category_cards(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    tap_card_quantity(chat, 1)
    chat.tap("ACT:addmore")
    assert chat.keys == ["carousel.body", "cards.footer"]
    assert "Nuts, Seeds & Snacks" in chat.carousel.body


def test_edit_cart_opens_the_existing_cart_editor(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    tap_card_quantity(chat, 2)
    chat.tap("ACT:edit")
    assert chat.keys == ["cart.edit"]
    assert [r.title for r in chat.rows] == ["Seed Mix"]


def test_checkout_from_the_confirmation_starts_checkout(make_engine, tmp_path):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    tap_card_quantity(chat, 1)
    chat.tap("CHECKOUT")
    assert chat.keys == ["checkout.type_address"]


# ------------------------------------------------------------------ languages


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_the_new_strings_exist_in_hindi_and_telugu(make_engine, tmp_path, lang):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat, lang=lang)
    start_add(chat)
    assert chat.keys == ["card.ask_qty"]
    tap_card_quantity(chat, 2)
    assert chat.keys == ["card.added"] and "₹698" in chat.body
    assert not any(ch.isascii() and ch.isalpha() for ch in chat.last[0].buttons[0].title)


# ------------------------------------------------------------------ "Add more" by origin


def test_add_more_after_a_single_match_search_card_shows_that_card_again(chat, backend):
    onboard(chat)
    chat.send("do you have makhana")  # one match: its card, no list
    assert chat.keys == ["product.card"]
    chat.tap(chat.cards[0].buttons[0].id)
    tap_card_quantity(chat, 1)
    chat.tap("ACT:addmore")
    assert chat.keys == ["product.card"]  # the search is re-run; its one card comes back
    assert chat.cards[0].body.splitlines()[0] == "Makhana Crunch"
    assert not chat.rows and chat.carousel is None


def test_add_more_after_a_search_result_row_shows_the_results_list_again(chat, backend):
    onboard(chat)
    chat.send("protein")  # several matches: a compact list
    assert chat.keys == ["choose.product"]  # first send: "I found several matches. Which one?"
    names_before = [r.title for r in chat.rows if r.id.startswith("ACT:showcard:")]
    chat.tap(next(r.id for r in chat.rows if r.id.startswith("ACT:showcard:")))
    chat.tap(chat.cards[0].buttons[0].id)
    tap_card_quantity(chat, 1)
    chat.tap("ACT:addmore")
    # Same rows, but re-shown through the listing's own renderer, so the intro line is the
    # page-range text ("Results: 1-N of M.") rather than the first-send wording.
    assert chat.keys == ["search.results"]
    assert chat.body.startswith("Results: 1–") and " of " in chat.body
    assert [r.title for r in chat.rows if r.id.startswith("ACT:showcard:")] == names_before
    assert not chat.cards and chat.carousel is None


def test_add_more_after_the_session_expired_falls_back_to_the_main_menu(
    make_engine, tmp_path, clock
):
    chat = make_chat(make_engine, tmp_path)
    onboard(chat)
    start_add(chat)
    tap_card_quantity(chat, 1)
    clock.advance(3601)  # the session, and the page it remembered, are gone
    chat.tap("ACT:addmore")
    assert chat.keys == ["menu.body"]  # the customer gets the main menu, not an error
    assert chat.carousel is None and not chat.cards
    assert cart_of(chat) == {"Seed Mix": 1}  # the cart itself lives in the Port: still there

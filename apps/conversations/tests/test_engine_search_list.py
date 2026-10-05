"""Transcripts: typed search results as a compact list; a row tap sends the product card.

Category browsing keeps cards (test_engine_browse / test_engine_cards); the "add N of X"
disambiguation list is covered in test_engine_cart.
"""

from dataclasses import replace

import pytest

from apps.conversations.backend_port import NotFound
from apps.conversations.channel import MAX_ROWS, OutList
from apps.conversations.renderers import render
from apps.iva_common.money import format_money

from .harness import WA_ID, onboard

SHOWCARD = "ACT:showcard:"


def result_rows(chat):
    return [r for r in chat.rows if r.id.startswith(SHOWCARD)]


def has_more(chat) -> bool:
    return any(r.id == "ACT:more" for r in chat.rows)


@pytest.fixture
def many(backend):
    """Make the search "sizzle" match ``n`` synthetic products (real diet/stock semantics)."""
    real = backend.list_products

    def install(n: int):
        base = real(search="makhana", diet=None, limit=1, offset=0)[0][0]
        items = [replace(base, id=f"syn-{i:02d}", name=f"Sizzle {i:02d}") for i in range(n)]

        def list_products(*, search=None, diet=None, limit=10, offset=0, **kw):
            if search == "sizzle":
                return items[offset : offset + limit], len(items)
            return real(search=search, diet=diet, limit=limit, offset=offset, **kw)

        backend.list_products = list_products

    return install


def test_single_result_sends_the_product_card_not_a_list(chat):
    onboard(chat)
    chat.send("do you have makhana")
    assert chat.keys == ["product.card"]
    assert not chat.rows
    assert chat.cards[0].body.splitlines()[0] == "Makhana Crunch"
    assert [b.title for b in chat.cards[0].buttons] == ["Add to cart", "Order now"]


def test_single_result_card_adds_one(chat, backend):
    onboard(chat)
    chat.send("do you have makhana")
    chat.tap(chat.cards[0].buttons[0].id)
    assert chat.keys == ["cart.added"]
    customer = backend.get_or_create_customer(WA_ID)
    assert [ln.quantity for ln in backend.get_cart(customer.id).lines] == [1]


def test_single_result_out_of_stock_says_so(chat, backend):
    onboard(chat)
    backend.set_stock("makhana-crunch", False)
    chat.send("do you have makhana")
    assert chat.keys == ["product.unavailable"]
    assert not chat.cards


def test_single_result_missing_at_send_time_is_reported_gone(chat, backend):
    onboard(chat)
    real = backend.get_product

    def gone(product_id):
        raise NotFound("gone")

    backend.get_product = gone
    chat.send("do you have makhana")
    backend.get_product = real
    assert chat.keys[0] == "cards.stale"
    assert not chat.cards


def test_single_result_outside_the_diet_shows_the_notice_then_the_card(chat):
    onboard(chat, diet="veg")
    chat.send("do you have kebab")  # only match is non-veg
    assert chat.keys == ["diet.hidden_all"]
    assert "ACT:showall" in chat.ids
    chat.tap("ACT:showall")
    assert chat.keys[0] == "product.card"
    assert chat.cards[0].body.startswith("Chicken Seekh Kebab Bites")


def test_single_match_after_the_diet_filter_shows_card_and_notice(chat):
    onboard(chat, diet="veg")
    chat.send("do you have quinoa")  # Grilled Chicken Quinoa Bowl is hidden, Quinoa is not
    assert chat.keys == ["product.card", "diet.hidden_some"]
    assert chat.cards[0].body.startswith("Quinoa")


def test_add_with_quantity_still_adds_that_quantity(chat, backend):
    onboard(chat, diet="veg")  # a single visible match: quantity path, not a card
    chat.send("add 2 quinoa")
    assert chat.keys[0] == "cart.added"
    customer = backend.get_or_create_customer(WA_ID)
    assert {ln.name: ln.quantity for ln in backend.get_cart(customer.id).lines} == {"Quinoa": 2}


def test_add_with_quantity_and_several_matches_keeps_the_quantity(chat, backend):
    onboard(chat)
    chat.send("add 2 quinoa")
    assert chat.keys == ["choose.product"]
    chat.tap("PRODUCT_SELECT:quinoa")
    customer = backend.get_or_create_customer(WA_ID)
    assert {ln.name: ln.quantity for ln in backend.get_cart(customer.id).lines} == {"Quinoa": 2}


def test_five_results_are_a_list_with_name_and_port_price(chat, backend):
    onboard(chat)
    chat.send("protein")
    assert chat.keys == ["choose.product"]
    assert not chat.cards
    rows = result_rows(chat)
    assert len(rows) == 5
    assert not has_more(chat)
    for row in rows:
        product = backend.get_product(row.id.removeprefix(SHOWCARD))
        assert row.title == product.name
        assert row.description == format_money(product.price, product.currency)


def test_ten_results_fit_without_show_more(chat):
    onboard(chat)
    chat.send("mix")
    assert len(result_rows(chat)) == 10
    assert not has_more(chat)
    assert len(chat.rows) == MAX_ROWS


def test_eleven_results_show_nine_and_show_more(chat, many):
    many(11)
    onboard(chat)
    chat.send("do you have sizzle")
    assert len(result_rows(chat)) == 9
    assert has_more(chat)
    assert len(chat.rows) == MAX_ROWS
    chat.tap("ACT:more")
    assert [r.title for r in result_rows(chat)] == ["Sizzle 09", "Sizzle 10"]
    assert not has_more(chat)


def test_many_results_page_through_without_gaps_or_repeats(chat, many):
    many(23)
    onboard(chat)
    chat.send("do you have sizzle")
    seen = [r.title for r in result_rows(chat)]
    while has_more(chat):
        chat.tap("ACT:more")
        assert len(chat.rows) <= MAX_ROWS
        seen += [r.title for r in result_rows(chat)]
    assert seen == [f"Sizzle {i:02d}" for i in range(23)]


def test_result_lists_render_within_whatsapp_limits(chat, many):
    many(30)
    onboard(chat)
    chat.send("do you have sizzle")
    message = next(m for m in chat.last if isinstance(m, OutList))
    rendered = render(message)
    rows = rendered["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) <= MAX_ROWS


def test_tapping_a_row_sends_that_products_card(chat):
    onboard(chat)
    chat.send("protein")
    row = next(r for r in result_rows(chat) if r.title == "Active Protein")
    chat.tap(row.id)
    assert chat.keys == ["product.card"]
    card = chat.cards[0]
    assert card.body.splitlines()[0] == "Active Protein"
    assert [b.title for b in card.buttons] == ["Add to cart", "Order now"]


def test_card_from_a_row_adds_to_the_cart(chat, backend):
    onboard(chat)
    chat.send("protein")
    chat.tap(result_rows(chat)[0].id)
    chat.tap(chat.cards[0].buttons[0].id)
    assert chat.keys == ["cart.added"]
    customer = backend.get_or_create_customer(WA_ID)
    assert len(backend.get_cart(customer.id).lines) == 1


def test_out_of_stock_since_listing_is_rechecked_on_tap(chat, backend):
    onboard(chat)
    chat.send("protein")
    row = next(r for r in result_rows(chat) if r.title == "Active Protein")
    backend.set_stock(row.id.removeprefix(SHOWCARD), False)
    chat.tap(row.id)
    assert chat.keys == ["product.unavailable"]
    assert "Active Protein" in chat.body
    assert not chat.cards


def test_unknown_product_on_tap_is_reported_as_gone(chat):
    onboard(chat)
    chat.send("protein")
    chat.tap(f"{SHOWCARD}no-such-product")
    assert chat.keys[0] == "cards.stale"


def test_out_of_stock_items_are_tagged_in_the_list(chat, backend):
    onboard(chat)
    backend.set_stock("active-protein", False)
    chat.send("protein")
    tagged = next(r for r in result_rows(chat) if r.title == "Active Protein")
    assert "out of stock" in tagged.description.lower()


def test_diet_hides_some_results_and_says_so(chat):
    onboard(chat, diet="veg")
    chat.send("do you have crunch")  # Egg Masala Crunch is hidden for veg
    assert chat.keys == ["choose.product", "diet.hidden_some"]
    assert {r.title for r in result_rows(chat)} == {"Makhana Crunch", "Jowar Crunch"}
    assert "1" in chat.message("diet.hidden_some").body


def test_show_anyway_after_diet_notice_lists_everything(chat):
    onboard(chat, diet="veg")
    chat.send("do you have crunch")
    chat.tap("ACT:showall")
    assert len(result_rows(chat)) == 3
    assert "diet.hidden_some" not in chat.keys


def test_category_browsing_still_uses_cards(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    assert chat.cards
    assert not result_rows(chat)

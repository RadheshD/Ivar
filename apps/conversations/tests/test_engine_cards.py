"""Transcripts: product cards -- images, add to cart, order now, stale buttons."""

import pytest

from apps.conversations.fake_backend import FakeBackend

from .harness import ADDRESS, WA_ID, Chat, onboard


def cart_of(backend):
    customer = backend.get_or_create_customer(WA_ID)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


@pytest.fixture
def image_backend():
    return FakeBackend(image_base_url="https://cdn.example.test/assets/products/")


@pytest.fixture
def image_chat(make_engine, image_backend):
    return Chat(make_engine(image_backend))


def test_card_has_image_header_when_the_backend_provides_one(image_chat):
    onboard(image_chat)
    image_chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")  # every product has an image: carousel
    card = next(c for c in image_chat.carousel.cards if c.body.startswith("Seed Mix"))
    assert card.image_url == "https://cdn.example.test/assets/products/eat/seed-mix.jpg"


def test_card_has_no_image_when_the_product_has_none(make_engine, tmp_path):
    from .harness import write_fixture

    def drop_image(data):
        next(p for p in data["products"] if p["id"] == "ivar-oats")["image_path"] = None

    path = write_fixture(tmp_path, drop_image)
    chat = Chat(make_engine(FakeBackend(path, image_base_url="https://cdn.example.test/p/")))
    onboard(chat)
    chat.tap("CATEGORY_SELECT:breakfast-grains")
    ivar_oats = next(c for c in chat.cards if c.body.startswith("Ivar Oats"))
    assert ivar_oats.image_url is None  # a text-only card, not dropped from the page


def test_card_has_no_image_when_no_base_url_is_configured(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    assert chat.cards and all(c.image_url is None for c in chat.cards)


def test_add_to_cart_from_a_card_offers_view_cart_and_continue_shopping(chat, backend):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    card = next(c for c in chat.cards if c.body.startswith("Seed Mix"))
    add_id = next(b.id for b in card.buttons if b.title == "Add to cart")
    chat.tap(add_id)
    assert chat.keys == ["cart.added"]
    assert "Seed Mix" in chat.body
    assert cart_of(backend) == {"Seed Mix": 1}
    assert chat.ids == ["VIEW_CART", "ACT:continue"]


def test_continue_shopping_shows_the_group_category_list_not_the_cards_again(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")  # under "Packaged Foods"
    card = chat.cards[0]
    add_id = next(b.id for b in card.buttons if b.title == "Add to cart")
    chat.tap(add_id)
    chat.tap("ACT:continue")
    assert chat.keys == ["group.body"]
    assert not chat.cards  # the cards already sent are never repeated
    assert {r.title for r in chat.rows} == {
        "Breakfast & Grains",
        "Protein & Active Nutrition",
        "Nuts, Seeds & Snacks",
        "Cooking Essentials",
        "Teas & Beverages",
        "Ready Mixes",
        "Energy Bites & Cookies",
    }


def test_continue_shopping_from_the_other_group(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:cafe-favourites")  # under "Fresh & Ready to Eat"
    card = chat.cards[0]
    add_id = next(b.id for b in card.buttons if b.title == "Add to cart")
    chat.tap(add_id)
    chat.tap("ACT:continue")
    assert chat.keys == ["group.body"]
    assert not chat.cards
    assert {r.title for r in chat.rows} == {
        "Café Favourites",
        "Protein Bowls",
        "Non-Veg Snacks",
        "Wellness Shots",
    }


def test_continue_shopping_after_session_expiry_falls_back_to_the_main_menu(chat, clock):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    card = chat.cards[0]
    add_id = next(b.id for b in card.buttons if b.title == "Add to cart")
    chat.tap(add_id)
    clock.advance(3601)  # session (and its listing) is gone
    chat.tap("ACT:continue")
    assert chat.keys == ["menu.body"]
    assert not chat.cards


def test_order_now_from_a_card_goes_straight_to_checkout_with_the_whole_cart(chat, backend):
    onboard(chat)
    chat.send("add 2 makhana")  # something already in the cart before Order now
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    card = next(c for c in chat.cards if c.body.startswith("Seed Mix"))
    order_id = next(b.id for b in card.buttons if b.title == "Order now")
    chat.tap(order_id)
    assert chat.keys == ["checkout.type_address"]
    assert cart_of(backend) == {"Makhana Crunch": 2, "Seed Mix": 1}

    chat.send(ADDRESS)
    assert chat.keys == ["checkout.summary", "checkout.confirm"]
    assert "Makhana Crunch" in chat.body and "Seed Mix" in chat.body  # the whole cart, not one line


def test_stale_add_to_cart_button_product_removed(chat):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    chat.tap("ACT:addcard:no-such-product")
    assert chat.keys[0] == "cards.stale"
    assert chat.keys[1] == "category.body"  # offers the category again, not just the menu
    assert "ACT:addcard:no-such-product" not in chat.ids


def test_stale_order_now_button_now_out_of_stock(chat, backend):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    backend.set_stock("seed-mix", False)
    chat.tap("ACT:ordernow:seed-mix")
    assert chat.keys[0] == "cards.stale"
    assert chat.keys[1] == "category.body"
    assert cart_of(backend) == {}  # nothing was added


def test_stale_button_after_session_expiry_does_not_crash(chat, clock):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    clock.advance(3601)  # session (and its listing) is gone
    chat.tap("ACT:addcard:no-such-product")
    assert chat.keys[0] == "cards.stale"
    assert chat.keys[-1] == "menu.body"  # no listing to reopen: falls back to the menu


def test_valid_button_after_session_expiry_still_works(chat, clock, backend):
    onboard(chat)
    chat.tap("CATEGORY_SELECT:nuts-seeds-snacks")
    clock.advance(3601)  # session gone, but the product itself is still real
    chat.tap("ACT:addcard:seed-mix")
    assert chat.keys == ["cart.added"]
    assert cart_of(backend) == {"Seed Mix": 1}

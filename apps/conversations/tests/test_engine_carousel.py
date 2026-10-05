"""Transcripts: category pages sent as one interactive media carousel."""

import pytest

from apps.conversations.channel import Button, CarouselCard, OutCarousel, OutProductCard
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.renderers import render

from .harness import Chat, onboard, resize_category, write_fixture

BASE = "https://cdn.example.test/assets/products/"
CAFE = "CATEGORY_SELECT:cafe-favourites"  # 7 products in the fixture, under "Fresh & Ready to Eat"


def chat_with(make_engine, tmp_path, mutate=None, *, base=BASE) -> Chat:
    path = write_fixture(tmp_path, mutate or (lambda data: None))
    return Chat(make_engine(FakeBackend(path, image_base_url=base)))


def cafe_of(count):
    return lambda data: resize_category(data, "cafe-favourites", count)


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(chat.whatsapp_id)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


def first_line(card):
    return card.body.split("\n")[0]


# ------------------------------------------------------------------ page sizes


def test_a_page_with_images_is_one_carousel_then_a_footer(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    assert chat.keys == ["carousel.body", "cards.footer"]
    assert chat.carousel.body == "Here are our Café Favourites"
    assert len(chat.carousel.cards) == 7
    assert not chat.cards  # no separate cards alongside it
    assert [b.title for b in chat.last[-1].buttons] == ["Menu"]  # all 7 shown: nothing more


def test_each_card_has_image_name_price_and_the_same_two_buttons(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    for card in chat.carousel.cards:
        assert card.image_url.startswith(BASE)
        name, price = card.body.split("\n")[:2]
        assert name and price.startswith("₹")
        assert [b.title for b in card.buttons] == ["Add to cart", "Order now"]
        assert card.buttons[0].id.startswith("ACT:addcard:")
        assert card.buttons[1].id.startswith("ACT:ordernow:")


def test_a_single_product_uses_a_plain_card(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(1))
    onboard(chat)
    chat.tap(CAFE)
    assert chat.carousel is None
    assert len(chat.cards) == 1
    assert chat.cards[0].image_url  # still an image card, just not a carousel
    assert chat.keys[-1] == "cards.footer"


def test_two_products_make_the_smallest_carousel(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(2))
    onboard(chat)
    chat.tap(CAFE)
    assert chat.keys == ["carousel.body", "cards.footer"]
    assert len(chat.carousel.cards) == 2


def test_ten_products_fill_one_carousel_with_no_show_more(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(10))
    onboard(chat)
    chat.tap(CAFE)
    assert len(chat.carousel.cards) == 10
    assert [b.title for b in chat.last[-1].buttons] == ["Menu"]


def test_eleven_products_page_ten_then_a_single_card(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(11))
    onboard(chat)
    chat.tap(CAFE)
    first = [first_line(c) for c in chat.carousel.cards]
    assert len(first) == 10
    assert chat.last[-1].buttons[0].id == "ACT:more"
    assert len(chat.last[-1].buttons) == 2  # Show more + Menu
    chat.tap("ACT:more")
    assert chat.carousel is None  # one product left: a plain card
    assert len(chat.cards) == 1
    assert first_line(chat.cards[0]) not in first  # never repeated, never dropped
    assert [b.title for b in chat.last[-1].buttons] == ["Menu"]


def test_twelve_products_second_page_is_a_two_card_carousel(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(12))
    onboard(chat)
    chat.tap(CAFE)
    first = {first_line(c) for c in chat.carousel.cards}
    chat.tap("ACT:more")
    second = {first_line(c) for c in chat.carousel.cards}
    assert len(second) == 2 and not first & second
    assert len(first | second) == 12


# ------------------------------------------------------------------ payload limits


def _check_payload(payload):
    interactive = payload["interactive"]
    assert payload["type"] == "interactive" and interactive["type"] == "carousel"
    assert 0 < len(interactive["body"]["text"]) <= 1024
    cards = interactive["action"]["cards"]
    assert 2 <= len(cards) <= 10
    assert len({len(c["action"]["buttons"]) for c in cards}) == 1  # same buttons on every card
    ids = []
    for index, card in enumerate(cards):
        assert card["card_index"] == index
        assert card["header"]["type"] == "image"
        assert card["header"]["image"]["link"].startswith("https://")
        assert 0 < len(card["body"]["text"]) <= 160
        assert card["body"]["text"].count("\n") <= 2
        for button in card["action"]["buttons"]:
            assert button["type"] == "quick_reply"
            assert 0 < len(button["quick_reply"]["title"]) <= 20
            assert 0 < len(button["quick_reply"]["id"]) <= 256
            ids.append(button["quick_reply"]["id"])
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("lang", ["en", "te", "hi"])
@pytest.mark.parametrize("count", [2, 10])
def test_rendered_payload_respects_the_documented_limits(make_engine, tmp_path, lang, count):
    chat = chat_with(make_engine, tmp_path, cafe_of(count))
    onboard(chat, lang)
    chat.tap(CAFE)
    _check_payload(render(chat.carousel))


def test_the_real_catalog_pages_render_valid_payloads(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    for category in ("nuts-seeds-snacks", "cafe-favourites", "wellness-shots", "protein-bowls"):
        chat.tap(f"CATEGORY_SELECT:{category}")
        _check_payload(render(chat.carousel))


def _buttons(n):
    return (Button(f"a:{n}", "Add to cart"), Button(f"o:{n}", "Order now"))


def test_long_product_text_is_cut_to_the_card_body_limit():
    long_name = "Extra " * 40
    card = CarouselCard("https://x.test/a.jpg", f"{long_name}\n₹1\nOut of stock", _buttons(1))
    other = CarouselCard("https://x.test/b.jpg", "B\n₹2", _buttons(2))
    payload = render(OutCarousel("Body", (card, other)))
    assert len(payload["interactive"]["action"]["cards"][0]["body"]["text"]) == 160


def test_carousel_construction_rules():
    cards = [CarouselCard("https://x.test/i.jpg", f"P{n}", _buttons(n)) for n in range(11)]
    with pytest.raises(ValueError):
        OutCarousel("x", tuple(cards[:1]))
    with pytest.raises(ValueError):
        OutCarousel("x", tuple(cards))
    assert OutCarousel("x", tuple(cards[:10]))
    odd = CarouselCard("https://x.test/i.jpg", "P", (Button("z", "Z"),))
    with pytest.raises(ValueError):
        OutCarousel("x", (cards[0], odd))
    with pytest.raises(ValueError):
        CarouselCard("http://x.test/i.jpg", "P", _buttons(0))
    with pytest.raises(ValueError):
        CarouselCard("https://x.test/i.jpg", "P", (*_buttons(0), Button("t", "Third")))


# ------------------------------------------------------------------ fallbacks


def no_image(product_id):
    def mutate(data):
        next(p for p in data["products"] if p["id"] == product_id)["image_path"] = None

    return mutate


def test_a_missing_image_sends_the_page_as_cards_never_dropping_a_product(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, no_image("ivar-muesli-bowl"))
    onboard(chat)
    chat.tap(CAFE)
    assert chat.carousel is None
    assert len(chat.cards) == 4 and chat.keys[0] == "category.body"
    seen = {first_line(c) for c in chat.cards}
    while "ACT:more" in chat.ids:
        chat.tap("ACT:more")
        seen |= {first_line(c) for c in chat.cards}
        if chat.carousel:  # the next page has images throughout: a carousel again
            seen |= {first_line(c) for c in chat.carousel.cards}
    assert len(seen) == 7  # all seven reachable


def test_no_base_url_means_cards_not_a_carousel(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, base="")
    onboard(chat)
    chat.tap(CAFE)
    assert chat.carousel is None and len(chat.cards) == 4


def test_the_carousel_carries_the_same_products_as_its_fallback(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(10))
    onboard(chat)
    chat.tap(CAFE)
    fallback = chat.carousel.fallback
    cards = [m for m in fallback if isinstance(m, OutProductCard)]
    assert len(cards) == 10
    assert [m.body for m in cards] == [c.body for c in chat.carousel.cards]
    assert [tuple(b.id for b in m.buttons) for m in cards] == [
        tuple(b.id for b in c.buttons) for c in chat.carousel.cards
    ]
    assert fallback[0].key == "category.body"


# ------------------------------------------------------------------ taps


def _button(chat, product_name, title):
    card = next(c for c in chat.carousel.cards if c.body.startswith(product_name))
    return next(b.id for b in card.buttons if b.title == title)


def test_add_to_cart_from_a_carousel_card(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    name = first_line(chat.carousel.cards[0])
    chat.tap(_button(chat, name, "Add to cart"))
    assert chat.keys == ["cart.added"]
    assert chat.ids == ["VIEW_CART", "ACT:continue"]
    assert cart_of(chat) == {name: 1}


def test_order_now_from_a_carousel_card(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    name = first_line(chat.carousel.cards[1])
    chat.tap(_button(chat, name, "Order now"))
    assert chat.keys == ["checkout.type_address"]
    assert cart_of(chat) == {name: 1}


def test_continue_shopping_after_a_carousel_tap_returns_to_the_group_list(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    name = first_line(chat.carousel.cards[0])
    chat.tap(_button(chat, name, "Add to cart"))
    chat.tap("ACT:continue")
    assert chat.keys == ["group.body"]
    assert chat.carousel is None and not chat.cards
    assert {r.title for r in chat.rows} == {
        "Café Favourites",
        "Protein Bowls",
        "Non-Veg Snacks",
        "Wellness Shots",
    }


def test_tap_rechecks_via_the_port_stale_product_relists_the_category(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    name = first_line(chat.carousel.cards[0])
    button = _button(chat, name, "Order now")
    chat.engine.backend.set_stock(chat.session().listing["ids"][0], False)
    chat.tap(button)
    assert chat.keys[0] == "cards.stale"
    assert chat.carousel is not None  # the page is re-offered, the product now tagged
    stale = next(c for c in chat.carousel.cards if first_line(c) == name)
    assert "Out of stock" in stale.body
    assert cart_of(chat) == {}


def test_carousel_button_after_session_expiry_still_works(make_engine, tmp_path, clock):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.tap(CAFE)
    clock.advance(3601)
    chat.tap("ACT:addcard:ivar-muesli-bowl")
    assert chat.keys == ["cart.added"]


def test_show_more_after_a_carousel_continues_the_listing(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, cafe_of(12))
    onboard(chat)
    chat.tap(CAFE)
    assert "ACT:more" in chat.ids
    chat.tap("ACT:more")
    assert chat.session().listing["offset"] == 10


# ------------------------------------------------------------------ diet filter


def two_non_veg(data):
    cafe = [p for p in data["products"] if p["category_id"] == "cafe-favourites"]
    for product in cafe[:2]:
        product["diet_type"] = "non_veg"


def test_diet_filter_still_announces_hidden_items_after_a_carousel(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path, two_non_veg)
    onboard(chat, diet="veg")
    chat.tap(CAFE)
    assert len(chat.carousel.cards) == 5
    assert chat.keys == ["carousel.body", "cards.footer", "diet.hidden_some"]
    assert "ACT:showall" in chat.ids
    chat.tap("ACT:showall")
    assert len(chat.carousel.cards) == 7  # nothing stays hidden once asked
    assert "diet.hidden_some" not in chat.keys


# ------------------------------------------------------------------ search unchanged


def test_search_results_still_use_the_list_and_a_lone_match_a_card(make_engine, tmp_path):
    chat = chat_with(make_engine, tmp_path)
    onboard(chat)
    chat.send("makhana")  # one match: a plain card
    assert chat.carousel is None and len(chat.cards) == 1
    chat.send("mix")  # several matches: the list
    assert chat.carousel is None and not chat.cards
    assert chat.keys[0] in ("search.results", "choose.product")
    assert chat.rows

"""Typed "reorder" / "repeat last order" / "past orders" (all languages), the order list's port
contract (newest first, default limit 5), and the reorder paths they reach."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.conversations.backend_port import BackendUnavailable
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.messages import (
    ORDER_HISTORY_WORDS,
    ORDER_STATUS_WORDS,
    REORDER_WORDS,
    en,
    hi,
    t,
    te,
)

from .harness import LANGUAGES, Chat, onboard
from .test_engine_order_history import customer_of, place
from .test_engine_reorder import (
    OATS,
    QUINOA,
    SEED,
    _cart_with_other_items,
    buttons_of,
    cart_of,
    ordered,
    rb,  # noqa: F401  (fixtures)
    rchat,  # noqa: F401
    rflaky,  # noqa: F401
    tap_button,
)

# One non-English phrase per supported language/script, as customers type them.
PHRASES = {
    "hindi": "दोबारा ऑर्डर",
    "hinglish": "dobara order",
    "telugu": "మళ్లీ ఆర్డర్",
    "tenglish": "malli order",
}
HISTORY_PHRASES = {
    "hindi": "पुराने ऑर्डर",
    "hinglish": "purane orders",
    "telugu": "పాత ఆర్డర్లు",
    "tenglish": "paata orders",
}


# ----------------------------------------------------------------------- Port: order list


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(hours=1)
        return self.now


@pytest.fixture
def timed():
    return FakeBackend(clock=Clock())


def _place_many(backend, customer, count):
    return [place(backend, customer).order_number for _ in range(count)]


def test_list_recent_orders_is_newest_first(timed):
    customer = timed.get_or_create_customer("910000000001")
    numbers = _place_many(timed, customer, 3)
    listed = [v.order_number for v in timed.list_recent_orders(customer.id)]
    assert listed == list(reversed(numbers))


def test_list_recent_orders_defaults_to_five(timed):
    customer = timed.get_or_create_customer("910000000001")
    numbers = _place_many(timed, customer, 7)
    listed = [v.order_number for v in timed.list_recent_orders(customer.id)]
    assert listed == list(reversed(numbers))[:5]


def test_list_recent_orders_honours_the_limit(timed):
    customer = timed.get_or_create_customer("910000000001")
    numbers = _place_many(timed, customer, 3)
    assert [v.order_number for v in timed.list_recent_orders(customer.id, limit=1)] == [numbers[-1]]


def test_list_recent_orders_of_a_new_customer_is_empty(timed):
    customer = timed.get_or_create_customer("910000000002")
    assert list(timed.list_recent_orders(customer.id)) == []


def test_list_recent_orders_never_includes_another_customers(timed):
    mine = timed.get_or_create_customer("910000000001")
    other = timed.get_or_create_customer("910000000002")
    place(timed, other)
    assert list(timed.list_recent_orders(mine.id)) == []


def test_order_detail_has_lines_total_status_and_date(timed):
    customer = timed.get_or_create_customer("910000000001")
    order = place(timed, customer, ((OATS, 2),)).order_number
    detail = timed.get_order_detail(customer.id, order)
    (line,) = detail.lines
    assert (line.product_id, line.variant_id, line.quantity) == ("ivar-oats", OATS, 2)
    assert line.name and line.unit_price > 0
    assert detail.total == line.line_total
    assert detail.order_status is not None and detail.placed_at is not None


# ----------------------------------------------------------------------- the words


def test_the_requested_english_phrases_are_covered():
    assert {"reorder", "order again", "repeat last order"} <= set(en.REORDER_WORDS)
    assert {"my orders", "order history", "past orders"} <= set(en.ORDER_HISTORY_WORDS)


def test_every_language_has_a_script_and_a_romanized_reorder_phrase():
    for module in (hi, te):
        words = module.REORDER_WORDS
        assert any(not w.isascii() for w in words)
        assert any(w.isascii() for w in words)


def test_reorder_words_never_overlap_the_history_or_status_words():
    assert not REORDER_WORDS & ORDER_HISTORY_WORDS
    assert not REORDER_WORDS & ORDER_STATUS_WORDS


@pytest.mark.parametrize("word", sorted(HISTORY_PHRASES.values()))
def test_each_language_history_phrase_opens_the_list(make_engine, word):
    from apps.ai.text import normalize

    assert normalize(word) in ORDER_HISTORY_WORDS


@pytest.mark.parametrize("kind, phrase", sorted(PHRASES.items()))
def test_a_non_english_phrase_reorders_the_latest_order(make_engine, rb, kind, phrase):  # noqa: F811
    chat = onboard(Chat(make_engine(rb)))
    ordered(rb, chat)
    chat.send(phrase)
    assert chat.keys == ["reorder.added", "cart.title", "cart.subtotal"]
    assert cart_of(rb, chat) == {OATS: 2, QUINOA: 1}


@pytest.mark.parametrize("word", ["reorder", "order again", "repeat last order"])
def test_the_english_phrases_reorder_the_latest_order(make_engine, rb, word):  # noqa: F811
    chat = onboard(Chat(make_engine(rb)))
    ordered(rb, chat)
    chat.send(word)
    assert chat.keys == ["reorder.added", "cart.title", "cart.subtotal"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_reorder_word_reaches_the_reorder_flow(make_engine, lang):
    for word in sorted(REORDER_WORDS):
        backend = FakeBackend()  # a fresh customer and cart for every word
        chat = onboard(Chat(make_engine(backend)), lang)
        ordered(backend, chat)
        chat.send(word)
        assert chat.keys[0] == "reorder.added", word


def test_reorder_uses_the_most_recent_order(make_engine, timed):
    chat = onboard(Chat(make_engine(timed)))
    customer = customer_of(chat)
    place(timed, customer, ((OATS, 1),))
    place(timed, customer, ((QUINOA, 3),))
    chat.send("repeat last order")
    assert cart_of(timed, chat) == {QUINOA: 3}


# ----------------------------------------------------------------------- reorder paths


def test_no_history_says_so_plainly_with_a_menu_button(make_engine, rb):  # noqa: F811
    chat = onboard(Chat(make_engine(rb)))
    chat.send("reorder")
    assert chat.keys == ["order.none"]
    assert cart_of(rb, chat) == {}
    assert [b.id for b in buttons_of(chat)] == ["CATEGORY_SELECT"]


def test_all_available_goes_into_an_empty_cart(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rchat.send("order again")
    assert rchat.keys == ["reorder.added", "cart.title", "cart.subtotal"]
    assert "CHECKOUT" in rchat.ids  # the existing cart summary and Checkout button


def test_one_unavailable_item_is_named_and_not_added(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rb.set_stock("quinoa", False)
    rchat.send("reorder")
    assert rchat.keys == ["reorder.preview"]
    assert t("en", "reorder.unavailable", names="Quinoa") in rchat.body
    assert cart_of(rb, rchat) == {}
    tap_button(rchat, t("en", "btn.add_to_cart"))
    assert cart_of(rb, rchat) == {OATS: 2}


def test_a_price_change_shows_old_and_new_then_adds_at_the_new_price(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.send("reorder")
    assert rchat.keys == ["reorder.preview"]
    price_line = t("en", "reorder.price_line", name="Ivar Oats", old="₹249", new="₹299")
    assert price_line in rchat.body
    assert cart_of(rb, rchat) == {}
    tap_button(rchat, t("en", "btn.add_to_cart"))
    assert rb.get_cart(customer_of(rchat).id).lines[0].unit_price == Decimal("299.00")


def test_a_non_empty_cart_asks_replace_or_add_first(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    _cart_with_other_items(rb, rchat)
    rchat.send("reorder")
    assert rchat.keys == ["reorder.preview"]
    assert [b.title for b in buttons_of(rchat)] == [
        t("en", "btn.replace_cart"),
        t("en", "btn.add_current"),
        t("en", "btn.cancel"),
    ]
    assert cart_of(rb, rchat) == {QUINOA: 1, SEED: 2}  # untouched until the customer picks
    tap_button(rchat, t("en", "btn.replace_cart"))
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}


def test_a_backend_outage_changes_nothing_and_shows_no_error_text(rb, rflaky, rchat):  # noqa: F811
    ordered(rb, rchat)
    rflaky.fail["list_recent_orders"] = BackendUnavailable("internal detail")
    rchat.send("reorder")
    assert "internal detail" not in rchat.body
    assert "reorder.added" not in rchat.keys
    assert cart_of(rb, rchat) == {}


# ----------------------------------------------------------------------- robustness


def test_a_greeting_mid_flow_is_not_read_as_a_reorder(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rchat.send("hi")
    assert "reorder.added" not in rchat.keys
    assert cart_of(rb, rchat) == {}


def test_a_greeting_at_the_address_step_is_not_saved_as_an_address(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rchat.send("reorder")
    rchat.tap("CHECKOUT")
    assert rchat.keys == ["checkout.ask_address"]
    rchat.send("Hi")
    assert rchat.keys == ["checkout.welcome_back"]
    assert [a.text for a in rb.list_addresses(customer_of(rchat).id) if a.text == "Hi"] == []


def test_reorder_at_the_address_step_is_not_saved_as_an_address(rb, rchat):  # noqa: F811
    ordered(rb, rchat)
    rchat.send("reorder")
    rchat.tap("CHECKOUT")
    rchat.send("reorder")
    assert all(a.text != "reorder" for a in rb.list_addresses(customer_of(rchat).id))


def test_a_sentence_containing_reorder_is_not_an_exact_match(make_engine, rb):  # noqa: F811
    chat = onboard(Chat(make_engine(rb)))
    ordered(rb, chat)
    chat.send("can I reorder the oats please")
    assert "reorder.added" not in chat.keys
    assert cart_of(rb, chat) == {}

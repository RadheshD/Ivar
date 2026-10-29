"""One-tap reorder: stateless ACT actions, current stock and prices, no silent swaps."""

import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from apps.conversations.backend_port import BackendUnavailable, NotFound, ProductUnavailable
from apps.conversations.catalog_order import PORT_MAX_QUANTITY
from apps.conversations.channel import OutButtons, OutText
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.messages import CATALOGS, t
from apps.conversations.renderers import BUTTON_TITLE_MAX, INTERACTIVE_BODY_MAX, render_all
from apps.iva_common.money import format_money

from .harness import LANGUAGES, OTHER_WA_ID, WA_ID, Chat, FlakyBackend, onboard, write_fixture
from .test_engine_order_history import customer_of, place

OATS, QUINOA, SEED = "ivar-oats--std", "quinoa--std", "seed-mix--std"
OATS_NAME, QUINOA_NAME, SEED_NAME = "Ivar Oats", "Quinoa", "Seed Mix"
ORDER = ((OATS, 2), (QUINOA, 1))


class CatalogBackend(FakeBackend):
    """A Fake Backend whose catalog can lose a variant or a product, and whose stored order
    lines can be hidden or made too large."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hidden_variants: set[str] = set()
        self.gone_products: set[str] = set()
        self.strip_lines = False
        self.quantity_override: int | None = None
        self.add_calls = 0

    def get_product(self, product_id):
        if product_id in self.gone_products:
            raise NotFound("Product not found.")
        product = super().get_product(product_id)
        if self.hidden_variants:
            kept = tuple(v for v in product.variants if v.id not in self.hidden_variants)
            product = replace(product, variants=kept)
        return product

    def add_item(self, customer_id, variant_id, quantity):
        self.add_calls += 1
        if variant_id in self.hidden_variants:
            raise NotFound("Variant not found.")
        return super().add_item(customer_id, variant_id, quantity)

    def get_order_detail(self, customer_id, order_number):
        detail = super().get_order_detail(customer_id, order_number)
        if detail is None:
            return None
        if self.strip_lines:
            return replace(detail, lines=())
        if self.quantity_override is not None:
            lines = tuple(replace(ln, quantity=self.quantity_override) for ln in detail.lines)
            return replace(detail, lines=lines)
        return detail


def _with_a_second_oats_variant(data):
    for product in data["products"]:
        if product["id"] == "ivar-oats":
            large = copy.deepcopy(product["variants"][0])
            large.update(id="ivar-oats--large", label="Large", price="599.00")
            product["variants"].append(large)


@pytest.fixture
def rb(tmp_path):
    return CatalogBackend(fixture_path=write_fixture(tmp_path, _with_a_second_oats_variant))


@pytest.fixture
def rflaky(rb):
    return FlakyBackend(rb)


@pytest.fixture
def rchat(make_engine, rflaky):
    return onboard(Chat(make_engine(rflaky)))


def cart_of(backend, chat) -> dict[str, int]:
    cart = backend.get_cart(customer_of(chat).id)
    return {line.variant_id: line.quantity for line in cart.lines}


def unit_prices(backend, chat) -> dict[str, Decimal]:
    cart = backend.get_cart(customer_of(chat).id)
    return {line.variant_id: line.unit_price for line in cart.lines}


def buttons_of(chat) -> list:
    return [b for m in chat.last if isinstance(m, OutButtons) for b in m.buttons]


def tap_button(chat, title: str):
    button = next(b for b in buttons_of(chat) if b.title == title)
    return chat.tap(button.id)


def ordered(rb, chat, items=ORDER):
    return place(rb, customer_of(chat), items).order_number


# ----------------------------------------------------------------------- the button


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_detail_view_offers_reorder_first_then_payment_then_menu(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    chat.tap(f"ORDER_STATUS:{order}")
    buttons = buttons_of(chat)
    assert [b.title for b in buttons] == [
        t(lang, "btn.reorder"),
        t(lang, "btn.payment_status"),
        t(lang, "btn.menu"),
    ]  # exactly WhatsApp's limit of three
    assert buttons[0].id == f"ACT:reorder:{order}" and len(buttons[0].id) < 200
    render_all(chat.last)  # every reply ID fits WhatsApp's limits


def test_a_paid_order_offers_reorder_and_menu_only(rb, rchat):
    order = ordered(rb, rchat)
    rb.simulate_payment_confirmed(order)
    rchat.tap(f"ORDER_STATUS:{order}")
    assert [b.title for b in buttons_of(rchat)] == [t("en", "btn.reorder"), t("en", "btn.menu")]


def test_no_reorder_button_when_the_order_has_no_stored_lines(rb, rchat):
    order = ordered(rb, rchat)
    rb.strip_lines = True
    rchat.tap(f"ORDER_STATUS:{order}")
    assert t("en", "btn.reorder") not in [b.title for b in buttons_of(rchat)]


def test_new_button_titles_fit_whatsapp_in_every_language():
    for lang in LANGUAGES:
        for key in ("btn.reorder", "btn.replace_cart", "btn.add_current"):
            assert len(CATALOGS[lang][key]) <= BUTTON_TITLE_MAX, (lang, key)


# ----------------------------------------------------------------------- one tap


@pytest.mark.parametrize("lang", LANGUAGES)
def test_an_empty_cart_and_everything_available_is_one_tap_into_the_cart(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    addresses_before = [a.id for a in rb.list_addresses(customer_of(chat).id)]
    orders_before = len(rb.list_recent_orders(customer_of(chat).id, limit=50))
    chat.tap(f"ACT:reorder:{order}")
    assert chat.keys == ["reorder.added", "cart.title", "cart.subtotal"]
    assert t(lang, "reorder.added", order=order) in chat.body
    assert cart_of(rb, chat) == {OATS: 2, QUINOA: 1}
    assert "CHECKOUT" in chat.ids  # the cart shows the Checkout button
    # nothing was checked out and the address was never touched
    assert [a.id for a in rb.list_addresses(customer_of(chat).id)] == addresses_before
    assert len(rb.list_recent_orders(customer_of(chat).id, limit=50)) == orders_before
    assert chat.session().address_id is None


def test_checkout_after_reorder_runs_the_normal_address_step(rb, rchat):
    order = ordered(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    rchat.tap("CHECKOUT")
    assert rchat.keys == ["checkout.ask_address"]  # the customer picks the address again


# ----------------------------------------------------------------------- the preview


@pytest.mark.parametrize("lang", LANGUAGES)
def test_an_unavailable_item_is_listed_and_never_added(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    rb.set_stock("quinoa", False)
    chat.tap(f"ACT:reorder:{order}")
    assert chat.keys == ["reorder.preview"]
    body = chat.body
    assert t(lang, "reorder.preview", order=order) in body
    assert t(lang, "reorder.unavailable", names=QUINOA_NAME) in body
    line = t(lang, "reorder.line", name=OATS_NAME, qty=2, total=format_money(Decimal("498")))
    assert line in body
    assert cart_of(rb, chat) == {}  # nothing is added before the customer chooses
    assert [b.title for b in buttons_of(chat)] == [
        t(lang, "btn.add_to_cart"),
        t(lang, "btn.cancel"),
    ]
    tap_button(chat, t(lang, "btn.add_to_cart"))
    assert cart_of(rb, chat) == {OATS: 2}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_price_change_shows_old_and_new_and_the_cart_gets_the_new_price(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    rb.set_price("ivar-oats", Decimal("299"))
    chat.tap(f"ACT:reorder:{order}")
    assert chat.keys == ["reorder.preview"]
    body = chat.body
    assert t(lang, "reorder.price_title") in body
    price_line = t(lang, "reorder.price_line", name=OATS_NAME, old="₹249", new="₹299")
    assert price_line in body
    assert t(lang, "reorder.line", name=OATS_NAME, qty=2, total="₹598") in body  # current price
    assert cart_of(rb, chat) == {}
    tap_button(chat, t(lang, "btn.add_to_cart"))
    assert unit_prices(rb, chat)[OATS] == Decimal("299.00")
    assert cart_of(rb, chat) == {OATS: 2, QUINOA: 1}


def test_a_variant_that_is_gone_is_unavailable_and_not_swapped(rb, rchat):
    order = ordered(rb, rchat)
    rb.hidden_variants.add(OATS)  # the "Large" oats are still on sale
    rchat.tap(f"ACT:reorder:{order}")
    assert t("en", "reorder.unavailable", names=OATS_NAME) in rchat.body
    tap_button(rchat, t("en", "btn.add_to_cart"))
    assert cart_of(rb, rchat) == {QUINOA: 1}
    assert "ivar-oats--large" not in cart_of(rb, rchat)


def test_a_product_that_no_longer_exists_is_unavailable(rb, rchat):
    order = ordered(rb, rchat)
    rb.gone_products.add("quinoa")
    rchat.tap(f"ACT:reorder:{order}")
    assert t("en", "reorder.unavailable", names=QUINOA_NAME) in rchat.body
    assert cart_of(rb, rchat) == {}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_nothing_available_leaves_the_cart_unchanged(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    customer = customer_of(chat)
    rb.add_item(customer.id, SEED, 1)
    rb.set_stock("ivar-oats", False)
    rb.set_stock("quinoa", False)
    chat.tap(f"ACT:reorder:{order}")
    assert chat.keys == ["reorder.nothing"]
    assert [b.title for b in buttons_of(chat)] == [t(lang, "btn.menu")]
    assert cart_of(rb, chat) == {SEED: 1}


def test_a_quantity_over_the_cart_limit_is_clamped_and_reported(rb, rchat):
    order = ordered(rb, rchat)
    rb.quantity_override = 25
    rchat.tap(f"ACT:reorder:{order}")
    assert rchat.keys == ["reorder.preview"]
    assert "Limited to" in rchat.body and OATS_NAME in rchat.body
    tap_button(rchat, t("en", "btn.add_to_cart"))
    assert cart_of(rb, rchat) == {OATS: PORT_MAX_QUANTITY, QUINOA: PORT_MAX_QUANTITY}


def test_a_long_preview_keeps_the_question_in_the_buttons_message(rb, rchat):
    items = []
    for product in rb.list_products(limit=50)[0]:
        detail = rb.get_product(product.id)
        if detail.in_stock and detail.variants[0].in_stock:
            items.append((detail.variants[0].id, 1))
    order = ordered(rb, rchat, tuple(items[:40]))
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.tap(f"ACT:reorder:{order}")
    assert rchat.keys == ["reorder.preview.details", "reorder.preview"]
    text, question = rchat.last
    assert isinstance(text, OutText) and isinstance(question, OutButtons)
    assert text.body.count("•") >= 40 and len(text.body) > INTERACTIVE_BODY_MAX - 300
    assert len(question.buttons) == 2


# ----------------------------------------------------------------------- the three buttons


def _cart_with_other_items(rb, chat):
    customer = customer_of(chat)
    rb.add_item(customer.id, QUINOA, 1)
    rb.add_item(customer.id, SEED, 2)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_cart_with_items_shows_replace_add_and_cancel(make_engine, rb, lang):
    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    _cart_with_other_items(rb, chat)
    chat.tap(f"ACT:reorder:{order}")  # even with nothing different, a cart with items asks
    assert chat.keys == ["reorder.preview"]
    assert t(lang, "reorder.cart_has_items", count=2) in chat.body
    assert [b.title for b in buttons_of(chat)] == [
        t(lang, "btn.replace_cart"),
        t(lang, "btn.add_current"),
        t(lang, "btn.cancel"),
    ]
    assert cart_of(rb, chat) == {QUINOA: 1, SEED: 2}  # nothing changed yet


def test_add_to_current_merges_into_the_cart(rb, rchat):
    order = ordered(rb, rchat)
    _cart_with_other_items(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    tap_button(rchat, t("en", "btn.add_current"))
    assert rchat.keys[0] == "reorder.added"
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 2, SEED: 2}


def test_replace_makes_the_cart_match_the_order(rb, rchat):
    order = ordered(rb, rchat)
    _cart_with_other_items(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    tap_button(rchat, t("en", "btn.replace_cart"))
    assert rchat.keys[0] == "reorder.replaced"
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}


def test_cancel_changes_nothing(rb, rchat):
    order = ordered(rb, rchat)
    _cart_with_other_items(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    tap_button(rchat, t("en", "btn.cancel"))
    assert rchat.keys == ["reorder.cancelled"]
    assert cart_of(rb, rchat) == {QUINOA: 1, SEED: 2}


# ----------------------------------------------------------------------- Replace never empties


def test_replace_keeps_the_cart_when_every_line_fails(rb, rflaky, rchat):
    order = ordered(rb, rchat, ((OATS, 1), (SEED, 1)))
    rb.add_item(customer_of(rchat).id, QUINOA, 3)
    rchat.tap(f"ACT:reorder:{order}")
    tap = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.replace_cart"))
    rflaky.fail["add_item"] = ProductUnavailable("went away")  # between the preview and the tap
    rchat.tap(tap.id)
    assert cart_of(rb, rchat) == {QUINOA: 3}  # not emptied
    assert rchat.keys[0] == "reorder.nothing"


def test_replace_keeps_the_cart_when_the_backend_fails_part_way(rb, rflaky, rchat):
    order = ordered(rb, rchat, ((OATS, 1), (SEED, 1)))
    rb.add_item(customer_of(rchat).id, QUINOA, 3)
    rchat.tap(f"ACT:reorder:{order}")
    tap = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.replace_cart"))
    rflaky.fail["add_item"] = BackendUnavailable("down")
    rchat.tap(tap.id)
    assert rchat.keys == ["error.unavailable"]
    assert cart_of(rb, rchat) == {QUINOA: 3}


def test_add_catches_a_quantity_error_per_line(rb, rchat):
    order = ordered(rb, rchat)
    customer = customer_of(rchat)
    rb.add_item(customer.id, OATS, 19)  # 19 + 2 is over the limit; the other line still goes in
    rchat.tap(f"ACT:reorder:{order}")
    tap_button(rchat, t("en", "btn.add_current"))
    assert cart_of(rb, rchat) == {OATS: 19, QUINOA: 1}
    assert "order.excluded_quantity" in rchat.keys


# ----------------------------------------------------------------------- re-validation


def test_a_choice_tap_revalidates_and_shows_a_new_preview_if_the_plan_changed(rb, rchat):
    order = ordered(rb, rchat)
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.tap(f"ACT:reorder:{order}")
    stale = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.add_to_cart"))
    rb.set_stock("quinoa", False)  # changes after the customer saw the preview
    rchat.tap(stale.id)
    assert rchat.keys == ["reorder.preview"]
    assert t("en", "reorder.unavailable", names=QUINOA_NAME) in rchat.body
    assert cart_of(rb, rchat) == {}  # nothing was applied
    tap_button(rchat, t("en", "btn.add_to_cart"))
    assert cart_of(rb, rchat) == {OATS: 2}


def test_a_choice_tap_after_the_customer_changed_the_cart_asks_again(rb, rchat):
    order = ordered(rb, rchat)
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.tap(f"ACT:reorder:{order}")
    stale = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.add_to_cart"))
    rb.add_item(customer_of(rchat).id, SEED, 1)
    rchat.tap(stale.id)
    assert rchat.keys == ["reorder.preview"] and cart_of(rb, rchat) == {SEED: 1}


# ----------------------------------------------------------------------- double tap


def test_a_second_one_tap_does_not_double_the_quantities(rb, rchat):
    order = ordered(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}
    rchat.tap(f"ACT:reorder:{order}")
    assert rchat.keys == ["cart.title", "cart.subtotal"]  # the cart is shown again
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}
    assert rchat.session().reorder_applied == order


def test_a_second_add_tap_does_not_double_the_quantities(rb, rchat):
    order = ordered(rb, rchat)
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.tap(f"ACT:reorder:{order}")
    add = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.add_to_cart"))
    calls_before = rb.add_calls
    rchat.tap(add.id)
    rchat.tap(add.id)
    assert rchat.keys == ["cart.title", "cart.subtotal"]
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}
    assert rb.add_calls - calls_before == 2  # one add_item per line, once


def test_a_second_tap_after_the_session_expired_asks_again_instead_of_doubling(rb, rchat):
    order = ordered(rb, rchat)
    rb.set_price("ivar-oats", Decimal("299"))
    rchat.tap(f"ACT:reorder:{order}")
    add = next(b for b in buttons_of(rchat) if b.title == t("en", "btn.add_to_cart"))
    rchat.tap(add.id)
    rchat.engine.store.delete(WA_ID)  # the session is gone, and so is the applied marker
    rchat.tap(add.id)
    assert rchat.keys == ["reorder.preview"]  # the cart changed since that preview
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}  # not doubled
    rchat.engine.store.delete(WA_ID)
    rchat.tap(f"ACT:reorder:{order}")  # the one-tap button again, session still gone
    assert rchat.keys == ["reorder.preview"] and cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}


def test_changing_the_cart_clears_the_applied_marker(rb, rchat):
    order = ordered(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    rchat.send("add 2 packets makhana")
    assert rchat.session().reorder_applied is None


# ----------------------------------------------------------------------- stateless and safe


def test_reorder_works_after_the_session_expired(rb, rchat):
    order = ordered(rb, rchat)
    rchat.engine.store.delete(WA_ID)
    rchat.tap(f"ACT:reorder:{order}")
    assert rchat.keys == ["reorder.added", "cart.title", "cart.subtotal"]
    assert cart_of(rb, rchat) == {OATS: 2, QUINOA: 1}


def test_a_crafted_id_for_another_customers_order_is_not_found(rb, rchat, make_engine):
    mine = ordered(rb, rchat)
    other = onboard(Chat(make_engine(rb), OTHER_WA_ID), name="Asha")
    for reply_id in (
        f"ACT:reorder:{mine}",
        f"ACT:reorder_add:{mine}:abcd1234",
        f"ACT:reorder_replace:{mine}:abcd1234",
    ):
        other.tap(reply_id)
        assert other.keys == ["order.not_found"], reply_id
        assert cart_of(rb, other) == {}


def test_an_unknown_order_is_not_found(rchat):
    rchat.tap("ACT:reorder:IVA9999")
    assert rchat.keys == ["order.not_found"]


def test_an_order_with_no_stored_lines_cannot_be_reordered(rb, rchat):
    order = ordered(rb, rchat)
    rb.strip_lines = True
    rchat.tap(f"ACT:reorder:{order}")
    assert rchat.keys == ["reorder.no_lines"]
    assert cart_of(rb, rchat) == {}


def test_an_id_without_an_order_is_ignored_safely(rchat):
    rchat.tap("ACT:reorder:")
    assert rchat.keys == ["fallback"]


def test_the_reply_ids_fit_whatsapp(rb, rchat):
    order = ordered(rb, rchat)
    _cart_with_other_items(rb, rchat)
    rchat.tap(f"ACT:reorder:{order}")
    for button in buttons_of(rchat):
        assert len(button.id) < 200
    render_all(rchat.last)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_reorder_message_is_in_the_customers_language(make_engine, rb, lang):
    from .harness import in_script

    chat = onboard(Chat(make_engine(rb)), lang)
    order = ordered(rb, chat)
    rb.set_stock("quinoa", False)
    chat.tap(f"ACT:reorder:{order}")
    assert in_script(chat.message("reorder.preview").body.split("\n")[0], lang)
    tap_button(chat, t(lang, "btn.cancel"))
    assert chat.keys == ["reorder.cancelled"] and in_script(chat.body, lang)

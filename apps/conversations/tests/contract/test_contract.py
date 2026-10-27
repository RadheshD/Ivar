"""Backend Port contract suite.

Runs against every adapter harness (see ``harness.py``). Tests use only port
operations and harness hooks, never adapter internals, so the backend team can run
them unchanged against their real adapter.
"""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from apps.conversations.backend_port import (
    MAX_LINE_QUANTITY,
    Address,
    BackendPort,
    CartEmpty,
    CartView,
    Category,
    CheckoutResult,
    CustomerRef,
    InvalidQuantity,
    NotAuthorized,
    NotFound,
    OrderDetail,
    OrderLine,
    OrderStatus,
    OrderStatusView,
    PaymentStatus,
    ProductDetail,
    ProductSummary,
    ProductUnavailable,
    port_operations,
)

BAD_QUANTITIES = [0, -1, MAX_LINE_QUANTITY + 1, 1.5, "2", True, None]


def unknown_id() -> str:
    return f"missing-{uuid.uuid4().hex}"


def key() -> str:
    return uuid.uuid4().hex


def all_products(backend, **filters) -> list[ProductSummary]:
    items, offset = [], 0
    while True:
        page, total = backend.list_products(limit=10, offset=offset, **filters)
        items.extend(page)
        offset += 10
        if offset >= total:
            return items


def place_order(backend, harness, customer, quantity=1) -> CheckoutResult:
    backend.add_item(customer.id, harness.in_stock_variant(), quantity)
    address = backend.add_address(customer.id, "12 Test Street, Hyderabad")
    return backend.checkout(customer.id, address.id, key())


# --------------------------------------------------------------------------- shape


def test_implements_every_operation(backend):
    assert isinstance(backend, BackendPort)
    for name in port_operations():
        assert callable(getattr(backend, name)), name
    assert len(port_operations()) == 18
    assert {"get_order_detail", "get_customer"} <= set(port_operations())


# --------------------------------------------------------------------------- customers


def test_get_or_create_customer_is_stable(backend, harness):
    wa_id = harness.new_whatsapp_id()
    first = backend.get_or_create_customer(wa_id)
    second = backend.get_or_create_customer(wa_id)
    assert isinstance(first, CustomerRef)
    assert first.id == second.id
    assert first.whatsapp_id == wa_id
    assert first.onboarding_complete is False


def test_different_whatsapp_ids_are_different_customers(customer, other_customer):
    assert customer.id != other_customer.id


def test_update_profile_partial(backend, customer):
    updated = backend.update_profile(customer.id, name="Asha", city="Hyderabad", language="te")
    assert (updated.name, updated.city, updated.language) == ("Asha", "Hyderabad", "te")
    updated = backend.update_profile(customer.id, diet="egg", onboarding_complete=True)
    assert updated.name == "Asha" and updated.language == "te"
    assert updated.diet == "egg" and updated.onboarding_complete is True
    again = backend.get_or_create_customer(customer.whatsapp_id)
    assert again == updated


def test_get_customer(backend, customer):
    found = backend.get_customer(customer.id)
    assert isinstance(found, CustomerRef)
    assert found == customer
    assert found.whatsapp_id == customer.whatsapp_id


def test_get_customer_unknown_is_none_and_never_creates(backend, harness):
    missing = unknown_id()
    assert backend.get_customer(missing) is None
    assert backend.get_customer(missing) is None  # still nothing: the lookup never creates
    wa_id = harness.new_whatsapp_id()
    created = backend.get_or_create_customer(wa_id)
    assert backend.get_customer(created.id) == created


def test_update_profile_unknown_customer(backend):
    with pytest.raises(NotFound):
        backend.update_profile(unknown_id(), name="X")


# --------------------------------------------------------------------------- catalog


def test_categories_tree(backend):
    groups = backend.list_categories()
    assert groups and all(isinstance(g, Category) and g.parent_id is None for g in groups)
    assert [g.sort_order for g in groups] == sorted(g.sort_order for g in groups)
    for group in groups:
        children = backend.list_categories(group.id)
        assert children
        assert all(c.parent_id == group.id for c in children)


def test_unknown_parent_category(backend):
    with pytest.raises(NotFound):
        backend.list_categories(unknown_id())


def test_list_products_pagination(backend):
    first, total = backend.list_products(limit=5)
    assert 0 < len(first) <= 5 and total >= len(first)
    items = all_products(backend)
    assert len(items) == total
    assert len({p.id for p in items}) == total


def test_list_products_offset_beyond_end(backend):
    _, total = backend.list_products(limit=1)
    page, again = backend.list_products(limit=10, offset=total)
    assert page == [] and again == total


def test_list_products_filter_by_category_and_group(backend):
    group = backend.list_categories()[0]
    category = backend.list_categories(group.id)[0]
    in_cat = all_products(backend, category_id=category.id)
    assert in_cat and all(p.category_id == category.id for p in in_cat)
    child_ids = {c.id for c in backend.list_categories(group.id)}
    in_group = all_products(backend, category_id=group.id)
    assert in_group and all(p.category_id in child_ids for p in in_group)


def test_list_products_unknown_category(backend):
    with pytest.raises(NotFound):
        backend.list_products(category_id=unknown_id())


@pytest.mark.parametrize(
    ("diet", "allowed"),
    [("veg", {"veg"}), ("egg", {"veg", "egg"}), ("non_veg", {"veg", "egg", "non_veg"})],
)
def test_list_products_diet_preference(backend, diet, allowed):
    items = all_products(backend, diet=diet)
    assert items and {p.diet_type for p in items} <= allowed
    if diet == "non_veg":
        assert len(items) == backend.list_products(limit=1)[1]


def test_list_products_search_and_tag(backend):
    sample = backend.list_products(limit=1)[0][0]
    word = sample.name.split()[0]
    found = all_products(backend, search=word.lower())
    assert sample.id in {p.id for p in found}
    assert all(word.lower() in p.name.lower() for p in found)
    tagged = all_products(backend, tag="high_protein")
    assert all("high_protein" in p.tags for p in tagged)


def test_money_is_decimal_everywhere(backend):
    for p in all_products(backend):
        assert isinstance(p.price, Decimal) and not isinstance(p.price, float)
        assert isinstance(p.currency, str) and p.currency
        assert p.protein_g is None or isinstance(p.protein_g, Decimal)
        assert isinstance(p.tags, tuple)


def test_get_product_detail(backend):
    summary = backend.list_products(limit=1)[0][0]
    detail = backend.get_product(summary.id)
    assert isinstance(detail, ProductDetail)
    assert detail.id == summary.id and detail.name == summary.name
    assert detail.variants
    for variant in detail.variants:
        assert isinstance(variant.price, Decimal)
        assert isinstance(variant.in_stock, bool)


def test_get_product_unknown(backend):
    with pytest.raises(NotFound):
        backend.get_product(unknown_id())


def test_resolve_product_names(backend):
    sample = backend.list_products(limit=1)[0][0]
    matches = backend.resolve_product_names(sample.name.upper())
    assert matches and matches[0].id == sample.id
    assert len(backend.resolve_product_names(sample.name.split()[0], limit=2)) <= 2
    assert backend.resolve_product_names("") == []
    assert backend.resolve_product_names("zzqqxxnotaproduct") == []


# --------------------------------------------------------------------------- cart


def test_new_cart_is_empty(backend, customer):
    cart = backend.get_cart(customer.id)
    assert isinstance(cart, CartView)
    assert cart.lines == () and cart.subtotal == Decimal("0")
    assert isinstance(cart.subtotal, Decimal)


def test_cart_lifecycle_and_totals(backend, harness, customer):
    v1, v2 = harness.in_stock_variant(), harness.other_in_stock_variant()
    cart = backend.add_item(customer.id, v1, 2)
    cart = backend.add_item(customer.id, v2, 1)
    assert len(cart.lines) == 2
    cart = backend.add_item(customer.id, v1, 3)  # same variant merges into one line
    assert len(cart.lines) == 2
    line1 = next(ln for ln in cart.lines if ln.variant_id == v1)
    assert line1.quantity == 5
    for line in cart.lines:
        assert isinstance(line.unit_price, Decimal) and isinstance(line.line_total, Decimal)
        assert line.line_total == line.unit_price * line.quantity
    assert cart.subtotal == sum((ln.line_total for ln in cart.lines), Decimal("0"))

    cart = backend.update_quantity(customer.id, line1.line_id, 1)
    assert next(ln for ln in cart.lines if ln.line_id == line1.line_id).quantity == 1
    assert cart.subtotal == sum((ln.line_total for ln in cart.lines), Decimal("0"))

    cart = backend.remove_item(customer.id, line1.line_id)
    assert [ln.variant_id for ln in cart.lines] == [v2]
    assert backend.get_cart(customer.id) == cart


@pytest.mark.parametrize("quantity", BAD_QUANTITIES)
def test_add_invalid_quantity(backend, harness, customer, quantity):
    with pytest.raises(InvalidQuantity):
        backend.add_item(customer.id, harness.in_stock_variant(), quantity)
    assert backend.get_cart(customer.id).lines == ()


def test_add_merge_beyond_max_is_invalid(backend, harness, customer):
    backend.add_item(customer.id, harness.in_stock_variant(), MAX_LINE_QUANTITY)
    with pytest.raises(InvalidQuantity):
        backend.add_item(customer.id, harness.in_stock_variant(), 1)
    assert backend.get_cart(customer.id).lines[0].quantity == MAX_LINE_QUANTITY


@pytest.mark.parametrize("quantity", BAD_QUANTITIES)
def test_update_invalid_quantity(backend, harness, customer, quantity):
    line = backend.add_item(customer.id, harness.in_stock_variant(), 1).lines[0]
    with pytest.raises(InvalidQuantity):
        backend.update_quantity(customer.id, line.line_id, quantity)


def test_add_unknown_variant(backend, customer):
    with pytest.raises(NotFound):
        backend.add_item(customer.id, unknown_id(), 1)


def test_add_out_of_stock(backend, harness, customer):
    with pytest.raises(ProductUnavailable):
        backend.add_item(customer.id, harness.out_of_stock_variant(), 1)


def test_unknown_cart_line(backend, customer):
    with pytest.raises(NotFound):
        backend.update_quantity(customer.id, unknown_id(), 1)
    with pytest.raises(NotFound):
        backend.remove_item(customer.id, unknown_id())


def test_other_customers_cart_line(backend, harness, customer, other_customer):
    line = backend.add_item(other_customer.id, harness.in_stock_variant(), 1).lines[0]
    with pytest.raises(NotAuthorized):
        backend.update_quantity(customer.id, line.line_id, 2)
    with pytest.raises(NotAuthorized):
        backend.remove_item(customer.id, line.line_id)
    assert backend.get_cart(other_customer.id).lines[0].quantity == 1
    assert backend.get_cart(customer.id).lines == ()


def test_cart_for_unknown_customer(backend):
    with pytest.raises(NotFound):
        backend.get_cart(unknown_id())


# --------------------------------------------------------------------------- addresses


def test_addresses(backend, customer, other_customer):
    assert backend.list_addresses(customer.id) == []
    address = backend.add_address(customer.id, "  Flat 4, Road 2, Hyderabad ", label="Home")
    assert isinstance(address, Address)
    assert address.text == "Flat 4, Road 2, Hyderabad" and address.label == "Home"
    assert backend.list_addresses(customer.id) == [address]
    assert backend.list_addresses(other_customer.id) == []


# --------------------------------------------------------------------------- checkout


def test_checkout_empty_cart(backend, customer):
    address = backend.add_address(customer.id, "1 Main Road")
    with pytest.raises(CartEmpty):
        backend.checkout(customer.id, address.id, key())
    assert backend.get_latest_order(customer.id) is None


def test_checkout_creates_pending_order(backend, harness, customer):
    cart = backend.add_item(customer.id, harness.in_stock_variant(), 2)
    address = backend.add_address(customer.id, "1 Main Road")
    result = backend.checkout(customer.id, address.id, key())
    assert isinstance(result, CheckoutResult)
    assert result.order_status == OrderStatus.PENDING
    assert result.payment_status == PaymentStatus.PENDING
    assert isinstance(result.total, Decimal) and result.total == cart.subtotal
    assert result.currency == cart.currency
    assert result.payment_url is None or result.payment_url.startswith("https://")
    assert backend.get_cart(customer.id).lines == ()


def test_checkout_is_idempotent(backend, harness, customer):
    backend.add_item(customer.id, harness.in_stock_variant(), 1)
    address = backend.add_address(customer.id, "1 Main Road")
    attempt = key()
    first = backend.checkout(customer.id, address.id, attempt)
    second = backend.checkout(customer.id, address.id, attempt)
    assert first == second
    assert len(backend.list_recent_orders(customer.id)) == 1


def test_new_key_new_order(backend, harness, customer):
    first = place_order(backend, harness, customer)
    second = place_order(backend, harness, customer)
    assert first.order_number != second.order_number


def test_checkout_other_customers_address(backend, harness, customer, other_customer):
    backend.add_item(customer.id, harness.in_stock_variant(), 1)
    foreign = backend.add_address(other_customer.id, "Somewhere else")
    with pytest.raises(NotAuthorized):
        backend.checkout(customer.id, foreign.id, key())
    assert backend.get_cart(customer.id).lines


def test_checkout_unknown_address(backend, harness, customer):
    backend.add_item(customer.id, harness.in_stock_variant(), 1)
    with pytest.raises(NotFound):
        backend.checkout(customer.id, unknown_id(), key())


# --------------------------------------------------------------------------- orders


def test_no_orders(backend, customer):
    assert backend.get_latest_order(customer.id) is None
    assert backend.list_recent_orders(customer.id) == []


def test_order_views(backend, harness, customer):
    first = place_order(backend, harness, customer)
    second = place_order(backend, harness, customer)

    latest = backend.get_latest_order(customer.id)
    assert isinstance(latest, OrderStatusView)
    assert latest.order_number == second.order_number
    assert latest.order_status == OrderStatus.PENDING
    assert latest.payment_status == PaymentStatus.PENDING
    assert latest.events and all(isinstance(e.at, datetime) for e in latest.events)
    assert all(e.at.tzinfo is not None for e in latest.events)

    one = backend.get_order(customer.id, first.order_number)
    assert one.order_number == first.order_number

    recent = backend.list_recent_orders(customer.id)
    assert [o.order_number for o in recent] == [second.order_number, first.order_number]
    assert len(backend.list_recent_orders(customer.id, limit=1)) == 1


def test_get_unknown_order(backend, customer):
    assert backend.get_order(customer.id, "IVA0000000") is None


def test_other_customers_order(backend, harness, customer, other_customer):
    order = place_order(backend, harness, other_customer)
    with pytest.raises(NotAuthorized):
        backend.get_order(customer.id, order.order_number)
    assert backend.get_latest_order(customer.id) is None
    assert backend.list_recent_orders(customer.id) == []


def test_order_views_carry_placed_at_total_and_currency(backend, harness, customer):
    placed = place_order(backend, harness, customer, quantity=2)
    for view in (
        backend.get_latest_order(customer.id),
        backend.get_order(customer.id, placed.order_number),
        backend.list_recent_orders(customer.id)[0],
    ):
        assert view.placed_at is not None and view.placed_at.tzinfo is not None
        assert view.placed_at == view.events[0].at
        assert isinstance(view.total, Decimal) and view.total == placed.total
        assert view.currency == placed.currency


def test_order_detail(backend, harness, customer):
    placed = place_order(backend, harness, customer, quantity=2)
    detail = backend.get_order_detail(customer.id, placed.order_number)
    assert isinstance(detail, OrderDetail)
    assert detail.order_number == placed.order_number
    assert detail.order_status == OrderStatus.PENDING
    assert detail.payment_status == PaymentStatus.PENDING
    assert isinstance(detail.total, Decimal) and detail.total == placed.total
    assert detail.currency == placed.currency
    assert detail.placed_at.tzinfo is not None
    assert isinstance(detail.lines, tuple)
    assert all(isinstance(line, OrderLine) for line in detail.lines)
    assert not hasattr(detail, "address") and not hasattr(detail, "address_id")


def test_order_detail_unknown_order_is_none(backend, customer):
    assert backend.get_order_detail(customer.id, "IVA0000000") is None


def test_order_detail_of_another_customer(backend, harness, customer, other_customer):
    order = place_order(backend, harness, other_customer)
    with pytest.raises(NotAuthorized):
        backend.get_order_detail(customer.id, order.order_number)


def test_order_detail_unknown_customer(backend):
    with pytest.raises(NotFound):
        backend.get_order_detail(unknown_id(), "IVA0000000")


def test_order_detail_lines_are_what_was_checked_out(backend, harness, customer):
    variant = harness.in_stock_variant()
    cart = backend.add_item(customer.id, variant, 2)
    address = backend.add_address(customer.id, "12 Test Street, Hyderabad")
    placed = backend.checkout(customer.id, address.id, key())
    detail = backend.get_order_detail(customer.id, placed.order_number)
    assert [line.variant_id for line in detail.lines] == [variant]
    [line] = detail.lines
    assert line.quantity == 2
    assert line.unit_price == cart.lines[0].unit_price
    assert line.line_total == line.unit_price * line.quantity
    assert detail.total == sum((ln.line_total for ln in detail.lines), Decimal("0"))

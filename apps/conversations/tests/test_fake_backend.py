"""Behaviour specific to the Fake Backend (numbering, fake URLs, test-only helpers)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.conversations.backend_port import (
    CheckoutRejected,
    NotAuthorized,
    NotFound,
    OrderStatus,
    PaymentStatus,
    payment_event,
)
from apps.conversations.fake_backend import FakeBackend

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class StepClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def fake():
    return FakeBackend(clock=StepClock())


@pytest.fixture
def customer(fake):
    return fake.get_or_create_customer("919876541234")


def order_for(fake, customer, variant="ivar-oats--std", quantity=2, idem="k1"):
    fake.add_item(customer.id, variant, quantity)
    address = fake.add_address(customer.id, "1 Main Road")
    return fake.checkout(customer.id, address.id, idem)


def test_order_numbers_and_fake_payment_url(fake, customer):
    first = order_for(fake, customer, idem="a")
    second = order_for(fake, customer, idem="b")
    assert (first.order_number, second.order_number) == ("IVA1001", "IVA1002")
    assert first.payment_url == "https://example.invalid/pay/IVA1001"
    assert first.total == Decimal("498.00")


def test_same_idempotency_key_same_order_even_after_new_cart(fake, customer):
    first = order_for(fake, customer, idem="same")
    fake.add_item(customer.id, "quinoa--std", 1)
    address = fake.list_addresses(customer.id)[0]
    again = fake.checkout(customer.id, address.id, "same")
    assert again == first
    assert len(fake.list_recent_orders(customer.id)) == 1
    assert fake.get_cart(customer.id).lines  # replay does not consume the new cart


def test_payment_confirmed(fake, customer):
    order = order_for(fake, customer)
    view = fake.simulate_payment_confirmed(order.order_number)
    assert view.payment_status == PaymentStatus.PAID
    assert view.order_status == OrderStatus.CONFIRMED
    assert [e.status for e in view.events] == [
        OrderStatus.PENDING,
        payment_event(PaymentStatus.PAID),
        OrderStatus.CONFIRMED,
    ]
    times = [e.at for e in view.events]
    assert times == sorted(times)
    assert fake.get_latest_order(customer.id) == view


def test_payment_failed_keeps_order_pending(fake, customer):
    order = order_for(fake, customer)
    view = fake.simulate_payment_failed(order.order_number)
    assert view.payment_status == PaymentStatus.FAILED
    assert view.order_status == OrderStatus.PENDING
    assert view.events[-1].status == "PAYMENT_FAILED"
    retried = fake.simulate_payment_confirmed(order.order_number)
    assert (retried.payment_status, retried.order_status) == ("PAID", "CONFIRMED")


def test_payment_confirm_twice_refused(fake, customer):
    order = order_for(fake, customer)
    fake.simulate_payment_confirmed(order.order_number)
    with pytest.raises(ValueError):
        fake.simulate_payment_confirmed(order.order_number)


def test_simulate_status_with_eta_and_tracking(fake, customer):
    order = order_for(fake, customer)
    eta = (T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    view = fake.simulate_status(
        order.order_number, "OUT_FOR_DELIVERY", eta_start=eta[0], eta_end=eta[1], tracking_id="T1"
    )
    assert view.order_status == OrderStatus.OUT_FOR_DELIVERY
    assert (view.eta_start, view.eta_end, view.tracking_id) == (eta[0], eta[1], "T1")
    with pytest.raises(ValueError):
        fake.simulate_status(order.order_number, "TELEPORTED")
    with pytest.raises(NotFound):
        fake.simulate_status("IVA9999", "PACKED")


def test_new_order_has_no_eta_or_tracking(fake, customer):
    order_for(fake, customer)
    view = fake.get_latest_order(customer.id)
    assert (view.eta_start, view.eta_end, view.tracking_id) == (None, None, None)


def test_order_number_lookup_is_case_insensitive(fake, customer):
    order_for(fake, customer)
    assert fake.get_order(customer.id, " iva1001 ").order_number == "IVA1001"


def test_set_stock_and_checkout_rejected(fake, customer):
    fake.add_item(customer.id, "ivar-oats--std", 1)
    fake.set_stock("ivar-oats", False)
    assert fake.get_product("ivar-oats").in_stock is False
    assert fake.get_product("ivar-oats").variants[0].in_stock is False
    address = fake.add_address(customer.id, "1 Main Road")
    with pytest.raises(CheckoutRejected) as exc:
        fake.checkout(customer.id, address.id, "k")
    assert exc.value.reason == CheckoutRejected.PRODUCT_UNAVAILABLE
    assert fake.get_cart(customer.id).lines  # cart kept so the customer can fix it
    fake.set_stock("ivar-oats", True)
    assert fake.checkout(customer.id, address.id, "k").order_status == "PENDING"


def test_out_of_stock_still_listed(fake):
    fake.set_stock("green-tea", False)
    page, _ = fake.list_products(search="green tea")
    assert [(p.id, p.in_stock) for p in page] == [("green-tea", False)]


def test_reset(fake, customer):
    order_for(fake, customer)
    fake.set_stock("ivar-oats", False)
    fake.reset()
    assert fake.get_product("ivar-oats").in_stock is True
    with pytest.raises(NotFound):
        fake.get_cart(customer.id)
    again = fake.get_or_create_customer("919876541234")
    assert again.id == "cust_1"
    assert order_for(fake, again).order_number == "IVA1001"


def test_resolve_product_names_token_match(fake):
    names = [p.name for p in fake.resolve_product_names("some millet please", limit=10)]
    assert names and all("millet" in n.lower() for n in names)
    exact = fake.resolve_product_names("quinoa")
    assert exact[0].name == "Quinoa"
    assert "Grilled Chicken Quinoa Bowl" in [p.name for p in exact]


def test_catalog_counts(fake):
    groups = fake.list_categories()
    assert [g.id for g in groups] == ["fresh", "packaged"]
    assert sum(len(fake.list_categories(g.id)) for g in groups) == 11
    assert fake.list_products(limit=1)[1] == 55
    assert fake.list_products(diet="veg", limit=1)[1] == 46
    assert fake.list_products(diet="egg", limit=1)[1] == 48
    assert fake.list_products(category_id="nuts-seeds-snacks", limit=1)[1] == 10


def test_list_products_argument_validation(fake):
    for kwargs in ({"limit": 0}, {"limit": 51}, {"offset": -1}, {"diet": "vegan"}):
        with pytest.raises(ValueError):
            fake.list_products(**kwargs)


def test_update_profile_validation(fake, customer):
    with pytest.raises(ValueError):
        fake.update_profile(customer.id, language="fr")
    with pytest.raises(ValueError):
        fake.update_profile(customer.id, diet="vegan")


def test_deterministic_across_instances():
    a, b = FakeBackend(), FakeBackend()
    assert a.list_products(limit=50) == b.list_products(limit=50)
    ca, cb = a.get_or_create_customer("911111111111"), b.get_or_create_customer("911111111111")
    assert ca == cb


def test_calls_are_logged_as_fake(fake, caplog):
    with caplog.at_level(logging.INFO, logger="apps.conversations.fake_backend"):
        c = fake.get_or_create_customer("919876541234")
        fake.get_cart(c.id)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert all(m.startswith("backend=fake op=") for m in messages)
    assert all("919876541234" not in m for m in messages)


def test_image_url_built_from_base_url_and_fixture_path(fake):
    with_base = FakeBackend(image_base_url="https://cdn.example.test/assets/products/")
    product = with_base.get_product("seed-mix")
    assert product.image_url == "https://cdn.example.test/assets/products/eat/seed-mix.jpg"
    summary = with_base.list_products(search="seed mix")[0][0]
    assert summary.image_url == product.image_url


def test_image_url_is_none_without_a_base_url(fake):
    assert fake.get_product("seed-mix").image_url is None  # no base_url given to `fake`


def test_every_fixture_product_has_an_image_path():
    with_base = FakeBackend(image_base_url="https://cdn.example.test/assets/products/")
    products, total = with_base.list_products(limit=50, offset=0)
    products += with_base.list_products(limit=50, offset=50)[0]
    assert total == len(products) == 55
    assert all(p.image_url for p in products)
    oats = with_base.get_product("ivar-oats")
    assert oats.image_url == "https://cdn.example.test/assets/products/eat/oats.jpg"


def test_image_url_is_none_when_a_product_has_no_image_path(tmp_path):
    from .harness import write_fixture

    def drop_image(data):
        next(p for p in data["products"] if p["id"] == "ivar-oats")["image_path"] = None

    path = write_fixture(tmp_path, drop_image)
    backend = FakeBackend(path, image_base_url="https://cdn.example.test/assets/products/")
    assert backend.get_product("ivar-oats").image_url is None


def test_image_base_url_must_be_https(fake):
    non_https = FakeBackend(image_base_url="http://cdn.example.test/assets/products/")
    assert non_https.get_product("seed-mix").image_url is None


def test_set_price_changes_product_variant_and_cart(fake, customer):
    fake.add_item(customer.id, "ivar-oats--std", 2)
    fake.set_price("ivar-oats", "260")
    product = fake.get_product("ivar-oats")
    assert product.price == Decimal("260.00")
    assert product.variants[0].price == Decimal("260.00")
    assert fake.get_cart(customer.id).subtotal == Decimal("520.00")
    with pytest.raises(NotFound):
        fake.set_price("no-such-product", "1")
    with pytest.raises(TypeError):
        fake.set_price("ivar-oats", 1.5)  # money is never float


def test_order_views_fill_placed_at_total_and_currency(fake, customer):
    order = order_for(fake, customer)
    first_event_at = fake.get_order(customer.id, order.order_number).events[0].at
    for view in (
        fake.get_order(customer.id, order.order_number),
        fake.get_latest_order(customer.id),
        fake.list_recent_orders(customer.id)[0],
    ):
        assert view.placed_at == first_event_at == T0 + timedelta(seconds=1)
        assert view.total == Decimal("498.00") and view.currency == "INR"


def test_order_detail_total_and_currency(fake, customer):
    order = order_for(fake, customer)
    detail = fake.get_order_detail(customer.id, order.order_number.lower())
    assert (detail.total, detail.currency) == (Decimal("498.00"), "INR")


def test_get_customer_does_not_create(fake):
    assert fake.get_customer("cust_99") is None
    assert fake.get_customer("cust_1") is None
    assert fake.get_or_create_customer("919876541234").id == "cust_1"  # first real one: cust_1


def multi_line_order(fake, customer, idem="multi"):
    fake.add_item(customer.id, "ivar-oats--std", 2)
    fake.add_item(customer.id, "quinoa--std", 1)
    fake.add_item(customer.id, "seed-mix--std", 3)
    address = fake.add_address(customer.id, "Flat 12, Green Park Road, Madhapur")
    return fake.checkout(customer.id, address.id, idem)


def test_order_detail_keeps_every_line_as_at_checkout(fake, customer):
    cart = None
    fake.add_item(customer.id, "ivar-oats--std", 2)
    fake.add_item(customer.id, "quinoa--std", 1)
    fake.add_item(customer.id, "seed-mix--std", 3)
    cart = fake.get_cart(customer.id)
    address = fake.add_address(customer.id, "1 Main Road")
    order = fake.checkout(customer.id, address.id, "k")
    detail = fake.get_order_detail(customer.id, order.order_number)
    assert len(detail.lines) == 3
    for line, cart_line in zip(detail.lines, cart.lines, strict=True):
        variant = fake.get_product(cart_line.product_id).variants[0]
        assert (line.product_id, line.variant_id) == (cart_line.product_id, cart_line.variant_id)
        assert line.name == cart_line.name
        assert line.variant_label == variant.label
        assert line.quantity == cart_line.quantity
        assert line.unit_price == cart_line.unit_price
        assert line.line_total == cart_line.line_total


def test_lines_are_decimal_and_add_up(fake, customer):
    order = multi_line_order(fake, customer)
    detail = fake.get_order_detail(customer.id, order.order_number)
    for line in detail.lines:
        assert isinstance(line.unit_price, Decimal) and isinstance(line.line_total, Decimal)
        assert isinstance(line.quantity, int)
        assert line.line_total == line.unit_price * line.quantity
    assert detail.total == sum((line.line_total for line in detail.lines), Decimal("0"))
    assert detail.total == order.total


def test_a_later_price_change_does_not_change_a_past_order(fake, customer):
    order = multi_line_order(fake, customer)
    before = fake.get_order_detail(customer.id, order.order_number)
    assert len(before.lines) == 3
    fake.set_price("ivar-oats", Decimal("1.00"))
    after = fake.get_order_detail(customer.id, order.order_number)
    assert after == before
    assert fake.get_order(customer.id, order.order_number).total == order.total


def test_no_address_text_is_stored_with_the_order(fake, customer):
    order = multi_line_order(fake, customer)
    detail = fake.get_order_detail(customer.id, order.order_number)
    assert "Green Park" not in repr(detail)
    assert "Green Park" not in json.dumps(fake._snapshot()["orders"])


def test_order_detail_of_another_customers_order(fake, customer):
    order = multi_line_order(fake, customer)
    other = fake.get_or_create_customer("919876549999")
    with pytest.raises(NotAuthorized):
        fake.get_order_detail(other.id, order.order_number)


def test_a_replayed_checkout_keeps_the_original_lines(fake, customer):
    order = multi_line_order(fake, customer, idem="same")
    fake.add_item(customer.id, "quinoa--std", 5)
    address = fake.list_addresses(customer.id)[0]
    assert fake.checkout(customer.id, address.id, "same") == order
    assert len(fake.get_order_detail(customer.id, order.order_number).lines) == 3

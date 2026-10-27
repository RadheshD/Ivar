"""Dev-only persistence of the Fake Backend's customer state (IVA_FAKE_BACKEND_STATE_FILE).

Every test uses its own temporary file; the developer's real state file is never touched.
"""

import json
import logging
import os
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.conversations import fake_backend
from apps.conversations.fake_backend import FakeBackend, clear_state_file, state_file_counts

WA = "919876500001"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "nested" / "fake_backend.json"


def populate(backend):
    customer = backend.get_or_create_customer(WA)
    backend.update_profile(
        customer.id, name="Ravi", city="Hyderabad", language="te", diet="veg",
        onboarding_complete=True,
    )  # fmt: skip
    backend.add_item(customer.id, "seed-mix--std", 2)
    address = backend.add_address(customer.id, "Flat 12, Green Park Road, Madhapur", "Home")
    backend.add_item(customer.id, "makhana-crunch--std", 1)
    result = backend.checkout(customer.id, address.id, "key-1")
    backend.simulate_payment_confirmed(result.order_number)
    backend.add_item(customer.id, "quinoa--std", 3)  # a fresh, still-open cart
    return customer, address, result


def test_state_survives_a_simulated_restart(path):
    first = FakeBackend(state_file=path)
    customer, address, result = populate(first)
    cart_before = first.get_cart(customer.id)
    order_before = first.get_order(customer.id, result.order_number)

    second = FakeBackend(state_file=path)  # "restart": a brand new object, same file
    again = second.get_or_create_customer(WA)
    assert again == first.get_or_create_customer(WA)
    assert (again.name, again.language, again.diet, again.onboarding_complete) == (
        "Ravi", "te", "veg", True
    )  # fmt: skip
    assert second.get_cart(customer.id) == cart_before
    assert second.list_addresses(customer.id) == [address]
    order = second.get_order(customer.id, result.order_number)
    assert order == order_before
    assert order.payment_status == "PAID" and len(order.events) == len(order_before.events)
    assert second.get_latest_order(customer.id).order_number == result.order_number
    # Idempotent checkout still replays instead of creating a second order.
    assert second.checkout(customer.id, address.id, "key-1") == result


def test_counters_continue_after_a_restart(path):
    first = FakeBackend(state_file=path)
    customer, address, result = populate(first)
    second = FakeBackend(state_file=path)
    other = second.get_or_create_customer("919876500002")
    assert other.id != customer.id
    second.add_item(other.id, "seed-mix--std", 1)
    second.add_address(other.id, "Another address, long enough")
    next_order = second.checkout(
        other.id, second.list_addresses(other.id)[0].id, "key-2"
    )  # fmt: skip
    assert next_order.order_number != result.order_number


def test_only_customer_state_is_saved_not_the_catalog(path):
    populate(FakeBackend(state_file=path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {
        "version",
        "seq",
        "customers",
        "carts",
        "addresses",
        "orders",
        "checkouts",
    }
    assert "products" not in saved and "categories" not in saved


def test_catalog_changes_are_not_persisted_and_dropped_products_leave_the_cart(path, tmp_path):
    first = FakeBackend(state_file=path)
    customer = first.get_or_create_customer(WA)
    first.add_item(customer.id, "seed-mix--std", 1)
    first.set_price("seed-mix", 999)  # catalog-side change: not part of customer state
    second = FakeBackend(state_file=path)
    assert second.get_product("seed-mix").price != first.get_product("seed-mix").price
    # A catalog without that product: the stale cart line is dropped, nothing crashes.
    trimmed = tmp_path / "catalog.json"
    data = json.loads(fake_backend.FIXTURE_PATH.read_text(encoding="utf-8"))
    data["products"] = [p for p in data["products"] if p["id"] != "seed-mix"]
    trimmed.write_text(json.dumps(data), encoding="utf-8")
    third = FakeBackend(trimmed, state_file=path)
    assert third.get_cart(customer.id).lines == ()


def test_missing_file_starts_empty_without_a_warning_and_is_created_on_first_change(path, caplog):
    with caplog.at_level(logging.WARNING):
        backend = FakeBackend(state_file=path)
    assert not caplog.records and not path.exists()
    backend.get_or_create_customer(WA)
    assert path.exists()
    assert state_file_counts(path)["customers"] == 1


@pytest.mark.parametrize(
    "content",
    ["{not json", "", "[]", '{"version": 99}', '{"version": 1, "seq": {}}'],
)
def test_corrupt_file_starts_empty_with_a_warning_and_no_crash(path, caplog, content):
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        backend = FakeBackend(state_file=path)
    assert any("state file unreadable" in r.getMessage() for r in caplog.records)
    assert not any(content and content in r.getMessage() for r in caplog.records)
    assert backend.list_products(limit=5)[1] > 0  # the catalog is fine
    customer = backend.get_or_create_customer(WA)  # and it keeps working
    assert customer.id == "cust_1"
    assert path.with_name(path.name + ".corrupt").read_text(encoding="utf-8") == content
    assert FakeBackend(state_file=path).get_or_create_customer(WA).id == "cust_1"


def test_unset_means_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = FakeBackend()  # settings value is "" under the test settings
    populate(backend)
    assert list(tmp_path.iterdir()) == []
    assert FakeBackend().get_or_create_customer(WA).id == "cust_1"  # nothing carried over


def test_the_setting_enables_persistence(path):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(path)):
        FakeBackend().get_or_create_customer(WA)
        assert FakeBackend().get_or_create_customer(WA).id == "cust_1"
    assert path.exists()


def test_an_explicit_empty_state_file_overrides_the_setting(path):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(path)):
        FakeBackend(state_file="").get_or_create_customer(WA)
    assert not path.exists()


def test_writes_are_atomic_a_failed_replace_keeps_the_old_file_and_does_not_crash(
    path, monkeypatch, caplog
):
    backend = FakeBackend(state_file=path)
    backend.get_or_create_customer(WA)
    good = path.read_text(encoding="utf-8")

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fake_backend.os, "replace", broken)
    with caplog.at_level(logging.ERROR):
        backend.get_or_create_customer("919876500002")  # no exception
    assert any("could not save" in r.getMessage() for r in caplog.records)
    assert path.read_text(encoding="utf-8") == good  # the committed file is intact
    assert json.loads(good)  # and valid


def test_a_save_leaves_no_temp_file_behind(path):
    populate(FakeBackend(state_file=path))
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_a_second_process_sees_changes_made_through_the_file(path):
    server = FakeBackend(state_file=path)
    worker = FakeBackend(state_file=path)
    customer = server.get_or_create_customer(WA)
    server.add_item(customer.id, "seed-mix--std", 1)
    assert [ln.quantity for ln in worker.get_cart(customer.id).lines] == [1]
    worker.add_item(customer.id, "seed-mix--std", 2)
    assert [ln.quantity for ln in server.get_cart(customer.id).lines] == [3]


def test_reset_clears_the_saved_state_and_a_running_server_notices(path):
    server = FakeBackend(state_file=path)
    populate(server)
    other = FakeBackend(state_file=path)
    clear_state_file(path)  # what reset_dev_data --confirm does, from another process
    assert state_file_counts(path) == {"customers": 0, "cart lines": 0, "addresses": 0, "orders": 0}
    assert server.get_or_create_customer(WA).id == "cust_1"  # the server did not resurrect it
    assert other.get_or_create_customer("919876500009").id == "cust_2"


def test_backend_reset_also_clears_the_file(path):
    backend = FakeBackend(state_file=path)
    populate(backend)
    backend.reset()
    assert state_file_counts(path)["customers"] == 0
    assert FakeBackend(state_file=path).get_or_create_customer(WA).id == "cust_1"


def test_constructing_a_backend_never_wipes_an_existing_file(path):
    populate(FakeBackend(state_file=path))
    before = path.read_bytes()
    FakeBackend(state_file=path)
    assert path.read_bytes() == before


def test_state_file_counts_are_plain_numbers_and_zero_for_junk(path):
    populate(FakeBackend(state_file=path))
    counts = state_file_counts(path)
    assert counts == {"customers": 1, "cart lines": 1, "addresses": 1, "orders": 1}
    assert state_file_counts(path.with_name("missing.json"))["customers"] == 0
    assert os.path.exists(path)


def test_order_lines_survive_a_restart(path):
    first = FakeBackend(state_file=path)
    customer, _, result = populate(first)
    before = first.get_order_detail(customer.id, result.order_number)
    assert len(before.lines) == 2
    second = FakeBackend(state_file=path)
    after = second.get_order_detail(customer.id, result.order_number)
    assert after.lines == before.lines
    assert all(isinstance(line.unit_price, Decimal) for line in after.lines)


def test_a_state_file_from_before_order_lines_still_loads(path, tmp_path):
    first = FakeBackend(state_file=path)
    customer, _, result = populate(first)
    old = json.loads(path.read_text(encoding="utf-8"))
    for order in old["orders"]:
        order.pop("lines", None)  # what a pre-change state file looks like
    copy = tmp_path / "old_state_copy.json"
    copy.write_text(json.dumps(old), encoding="utf-8")
    assert "lines" not in copy.read_text(encoding="utf-8")
    loaded = FakeBackend(state_file=copy)
    detail = loaded.get_order_detail(customer.id, result.order_number)
    assert detail.lines == ()
    assert detail.total == result.total
    assert loaded.get_order(customer.id, result.order_number).placed_at is not None

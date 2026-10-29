"""``simulate_catalog_change``: a dev-only way to change a product's stock or price in the Fake
Backend's dev state file. Every test uses a temporary file; the real one is never touched."""

import os
import time
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.conversations.fake_backend import FakeBackend, state_file_counts

WA = "919876500001"
SECRET_MARKER = "app-secret-marker-xyz"


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "fake_backend.json"
    backend = FakeBackend(state_file=path)
    customer = backend.get_or_create_customer(WA)
    backend.add_item(customer.id, "seed-mix--std", 1)
    old = time.time() - 600
    os.utime(path, (old, old))  # not "just written": the server-is-writing check stays quiet
    return path


@pytest.fixture
def dev(state_path):
    with override_settings(
        IVA_DEV_RESET_ALLOWED=True,
        IVA_FAKE_BACKEND_STATE_FILE=str(state_path),
        WHATSAPP_APP_SECRET=SECRET_MARKER,
    ):
        yield state_path


def age(path):
    """Make the file look untouched for a while (the command just wrote it)."""
    old = time.time() - 600
    os.utime(path, (old, old))


def run(*args) -> str:
    out = StringIO()
    call_command("simulate_catalog_change", *args, stdout=out)
    return out.getvalue()


def product(path, product_id):
    return FakeBackend(state_file=path).get_product(product_id)


# ----------------------------------------------------------------------- guards


def test_refuses_unless_dev_reset_is_allowed(state_path):
    with override_settings(
        IVA_DEV_RESET_ALLOWED=False, IVA_FAKE_BACKEND_STATE_FILE=str(state_path)
    ):
        with pytest.raises(CommandError, match="IVA_DEV_RESET_ALLOWED"):
            run("quinoa", "--out-of-stock")
    assert product(state_path, "quinoa").in_stock is True


def test_refuses_with_a_real_backend_adapter(dev):
    with override_settings(IVA_BACKEND_ADAPTER="ivar_backend.adapter.RealAdapter"):
        with pytest.raises(CommandError, match="Fake Backend"):
            run("quinoa", "--out-of-stock")
    assert product(dev, "quinoa").in_stock is True


def test_refuses_without_a_state_file(dev):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=""):
        with pytest.raises(CommandError, match="IVA_FAKE_BACKEND_STATE_FILE"):
            run("quinoa", "--out-of-stock")


def test_refuses_when_the_state_file_was_written_a_moment_ago(dev):
    now = time.time()
    os.utime(dev, (now, now))  # a running server may be saving it
    with pytest.raises(CommandError, match="written"):
        run("quinoa", "--out-of-stock")
    assert product(dev, "quinoa").in_stock is True


def test_exactly_one_change_is_required(dev):
    with pytest.raises(CommandError):
        run("quinoa")
    with pytest.raises(CommandError):
        run("quinoa", "--out-of-stock", "--in-stock")
    with pytest.raises(CommandError):
        run("quinoa", "--out-of-stock", "--price", "100")


def test_an_unknown_product_is_refused(dev):
    with pytest.raises(CommandError, match="not in the catalog"):
        run("no-such-product", "--out-of-stock")


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5x", "nan"])
def test_a_bad_price_is_refused(dev, bad):
    with pytest.raises(CommandError, match="price"):
        run("quinoa", "--price", bad)
    assert product(dev, "quinoa").price == Decimal("449.00")


# ----------------------------------------------------------------------- effects


def test_out_of_stock_then_back_in_stock(dev):
    out = run("quinoa", "--out-of-stock")
    assert "quinoa" in out and "out of stock" in out
    assert product(dev, "quinoa").in_stock is False
    age(dev)
    run("quinoa", "--in-stock")
    assert product(dev, "quinoa").in_stock is True


def test_a_price_change_is_saved_as_a_decimal(dev):
    run("seed-mix", "--price", "299.50")
    assert product(dev, "seed-mix").price == Decimal("299.50")
    assert product(dev, "seed-mix").variants[0].price == Decimal("299.50")


def test_customer_state_is_left_alone(dev):
    before = state_file_counts(dev)
    run("quinoa", "--out-of-stock")
    assert (
        state_file_counts(dev)
        == before
        == {
            "customers": 1,
            "cart lines": 1,
            "addresses": 0,
            "orders": 0,
        }
    )


def test_dry_run_changes_nothing(dev):
    before = dev.read_bytes()
    out = run("quinoa", "--price", "10", "--dry-run")
    assert "dry run" in out.lower() and "quinoa" in out
    assert dev.read_bytes() == before
    assert product(dev, "quinoa").price == Decimal("449.00")


def test_a_dry_run_still_checks_the_product(dev):
    with pytest.raises(CommandError, match="not in the catalog"):
        run("no-such-product", "--in-stock", "--dry-run")


def test_a_dry_run_does_not_mind_a_recent_write(dev):
    now = time.time()
    os.utime(dev, (now, now))
    assert "dry run" in run("quinoa", "--out-of-stock", "--dry-run").lower()


def test_the_output_never_shows_secrets_or_paths_or_ids(dev):
    out = run("quinoa", "--out-of-stock") + run("quinoa", "--in-stock", "--dry-run")
    assert SECRET_MARKER not in out and WA not in out
    assert str(dev.parent) not in out  # only the file name is shown

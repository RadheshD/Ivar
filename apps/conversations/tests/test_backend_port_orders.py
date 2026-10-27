"""Shapes of the order types the Backend Port exposes for history and reorder."""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.conversations.backend_port import (
    OrderDetail,
    OrderLine,
    OrderStatusEvent,
    OrderStatusView,
)

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def test_order_status_view_still_builds_without_the_new_fields():
    view = OrderStatusView("IVA1001", "PENDING", "PENDING", (), None, None, None)
    assert (view.placed_at, view.total, view.currency) == (None, None, None)


def test_order_status_view_new_fields_are_optional_keywords_at_the_end():
    names = [f.name for f in dataclasses.fields(OrderStatusView)]
    assert names[-3:] == ["placed_at", "total", "currency"]
    view = OrderStatusView(
        "IVA1001", "PENDING", "PENDING", (OrderStatusEvent("PENDING", NOW),), None, None, None,
        placed_at=NOW, total=Decimal("99.00"), currency="INR",
    )  # fmt: skip
    assert view.total == Decimal("99.00")


def test_order_line_fields_and_frozen():
    line = OrderLine("p", "p--std", "Oats", None, 2, Decimal("10.00"), Decimal("20.00"))
    assert [f.name for f in dataclasses.fields(OrderLine)] == [
        "product_id", "variant_id", "name", "variant_label",
        "quantity", "unit_price", "line_total",
    ]  # fmt: skip
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.quantity = 3


def test_order_detail_fields_frozen_and_has_no_address():
    names = [f.name for f in dataclasses.fields(OrderDetail)]
    assert names == [
        "order_number", "placed_at", "total", "currency",
        "order_status", "payment_status", "lines",
    ]  # fmt: skip
    detail = OrderDetail("IVA1001", NOW, Decimal("0"), "INR", "PENDING", "PENDING", ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        detail.total = Decimal("1")

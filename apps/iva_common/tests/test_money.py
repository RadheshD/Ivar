from decimal import Decimal

import pytest

from apps.iva_common.money import format_inr, format_money, quantize, to_decimal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("249"), "₹249"),
        ("249.00", "₹249"),
        (1249, "₹1,249"),
        ("1249.5", "₹1,249.50"),
        ("100000", "₹1,00,000"),
        ("12345678.9", "₹1,23,45,678.90"),
        ("0", "₹0"),
        ("-50", "-₹50"),
    ],
)
def test_format_inr(value, expected):
    assert format_inr(value) == expected


def test_format_other_currency():
    assert format_money("10", "USD") == "USD 10"


@pytest.mark.parametrize("bad", [1.5, True])
def test_rejects_float_and_bool(bad):
    with pytest.raises(TypeError):
        to_decimal(bad)


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity"])
def test_rejects_invalid(bad):
    with pytest.raises(ValueError):
        to_decimal(bad)


def test_quantize_half_up():
    assert quantize("2.345") == Decimal("2.35")
    assert isinstance(quantize(3), Decimal)

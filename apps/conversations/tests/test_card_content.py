"""Pure tests: card body building, truncation, missing tags, typed quantity parsing."""

from decimal import Decimal

import pytest

from apps.conversations import card_content
from apps.conversations.card_content import (
    CARD_BODY_MAX,
    build_card_body,
    format_protein,
    parse_card_quantity,
)

MAX_QTY = 20  # a stand-in maximum: the parser takes it as an argument


def test_body_is_name_price_then_tags_on_three_lines():
    body = build_card_body("Seed Mix", "₹199", ["Veg", "Protein: 12 g"])
    assert body == "Seed Mix\n₹199\nVeg · Protein: 12 g"


def test_missing_tags_are_left_out_never_invented():
    assert build_card_body("Seed Mix", "₹199", ["Veg"]) == "Seed Mix\n₹199\nVeg"
    assert build_card_body("Seed Mix", "₹199", []) == "Seed Mix\n₹199"
    assert build_card_body("Seed Mix", "₹199", ["", "  "]) == "Seed Mix\n₹199"


def test_format_protein_handles_missing_and_trailing_zeros():
    assert format_protein(None) is None
    assert format_protein(Decimal("12.0")) == "12"
    assert format_protein(Decimal("7.50")) == "7.5"
    assert format_protein(Decimal("NaN")) is None


def test_a_long_name_is_cut_with_an_ellipsis_and_the_body_fits():
    name = "Super Long Product Name " * 20
    body = build_card_body(name, "₹199", ["Veg", "Protein: 12 g"])
    assert len(body) <= CARD_BODY_MAX
    first = body.split("\n")[0]
    assert first.endswith("…") and first.startswith("Super Long Product Name")
    assert body.endswith("\n₹199\nVeg · Protein: 12 g")  # price and tags survive intact


def test_a_name_that_fits_is_never_touched():
    body = build_card_body("A" * 100, "₹1", [])
    assert body == "A" * 100 + "\n₹1" and "…" not in body


def test_truncation_never_splits_a_multibyte_character():
    body = build_card_body("मखाना क्रंच " * 30, "₹199", ["शाकाहारी"])
    assert len(body) <= CARD_BODY_MAX and "\n₹199\nशाकाहारी" in body


def test_oversized_tags_are_cut_before_the_price_is():
    body = build_card_body("Oats", "₹99", ["T" * 300])
    assert len(body) <= CARD_BODY_MAX
    assert body.split("\n")[1] == "₹99" and body.startswith("Oats\n")


def test_never_more_than_two_line_breaks_even_with_extra_lines():
    body = build_card_body("Oats", "₹99", ["Veg"], ["Good for breakfast", "Another line"])
    assert body.count("\n") <= 2 and len(body) <= CARD_BODY_MAX
    assert "Good for breakfast" in body  # folded onto the last line, not dropped


def test_extra_line_providers_are_empty_by_default():
    assert card_content.EXTRA_LINE_PROVIDERS == ()  # no benefit line until someone adds one


@pytest.mark.parametrize("text", ["1", "2", "3", " 7 ", "20", "٣", "०२"])
def test_digits_in_range_are_accepted(text):
    parsed = parse_card_quantity(text, MAX_QTY)
    assert parsed.status == "ok" and 1 <= parsed.value <= MAX_QTY


def test_number_words_are_accepted():
    assert parse_card_quantity("two", MAX_QTY).value == 2


@pytest.mark.parametrize("text", ["0", "00"])
def test_zero_is_invalid(text):
    assert parse_card_quantity(text, MAX_QTY).status == "invalid"


@pytest.mark.parametrize(
    "text", ["21", "500", "99999", "9" * 40, "9" * 5000], ids=["21", "500", "99999", "40d", "5000d"]
)
def test_very_large_numbers_are_too_large_and_never_crash(text):
    assert parse_card_quantity(text, MAX_QTY).status == "too_large"


@pytest.mark.parametrize("text", ["", None, "menu", "view cart", "add 2 oats", "-3", "2.5", "abc"])
def test_anything_else_is_not_a_bare_number(text):
    assert parse_card_quantity(text, MAX_QTY).status == "none"

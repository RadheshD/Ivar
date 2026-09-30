"""The development catalog fixture matches the specification table exactly."""

import json
from collections import Counter
from decimal import Decimal

import pytest

from apps.conversations.fake_backend import FIXTURE_PATH

# Category | Product | Price | Protein g | kcal | Diet | Pack  (copied from the spec)
SPEC_TABLE = """
Breakfast & Grains | Ivar Oats | 249 | 6 | 180 | veg |
Breakfast & Grains | Multigrain Breakfast Mix | 329 | 7 | 200 | veg |
Breakfast & Grains | Ragi Mix | 299 | 5 | 190 | veg |
Breakfast & Grains | Millet Mix | 289 | 6 | 185 | veg |
Protein & Active Nutrition | Plant Protein Mix | 899 | 24 | 130 | veg |
Protein & Active Nutrition | Active Protein | 949 | 25 | 140 | veg |
Protein & Active Nutrition | Ivar Active Nutrition Drinks | 79 | | 40 | veg |
Nuts, Seeds & Snacks | Seed Mix | 349 | 6 | 160 | veg |
Nuts, Seeds & Snacks | Roasted Nuts | 399 | 6 | 170 | veg |
Nuts, Seeds & Snacks | Trail Mix | 349 | 5 | 165 | veg |
Nuts, Seeds & Snacks | Makhana Crunch | 199 | 4 | 120 | veg |
Nuts, Seeds & Snacks | Roasted Chana Mix | 179 | 7 | 140 | veg |
Nuts, Seeds & Snacks | Ragi Crisps | 189 | 3 | 145 | veg |
Nuts, Seeds & Snacks | Millet Puffs | 179 | 3 | 110 | veg |
Nuts, Seeds & Snacks | Millet & Seed Crackers | 219 | 4 | 150 | veg |
Nuts, Seeds & Snacks | Jowar Crunch | 179 | 3 | 140 | veg |
Nuts, Seeds & Snacks | Jowar Puffs | 179 | 3 | 115 | veg |
Cooking Essentials | Multigrain Atta | 249 | 12 | 340 | veg | 1 kg
Cooking Essentials | Quinoa | 449 | 13 | 360 | veg | 500 g
Cooking Essentials | Chia Seeds | 299 | 5 | 140 | veg |
Cooking Essentials | Flax Seeds | 199 | 5 | 150 | veg | 200 g
Teas & Beverages | Herbal Tea | 299 | | 5 | veg |
Teas & Beverages | Green Tea | 279 | | 2 | veg |
Teas & Beverages | Turmeric Latte | 329 | 2 | 90 | veg |
Ready Mixes | Instant Porridge Mix | 279 | 5 | 180 | veg |
Ready Mixes | Pancake Mix | 299 | 6 | 210 | veg |
Ready Mixes | Smoothie Mix | 349 | 4 | 170 | veg |
Café Favourites | Classic Oats Bowl | 199 | 7 | 220 | veg |
Café Favourites | Ivar Muesli Bowl | 219 | 7 | 240 | veg |
Café Favourites | Millet Pancakes | 249 | 6 | 230 | veg |
Café Favourites | Ragi Upma | 179 | 5 | 210 | veg |
Café Favourites | Millet Dosa | 149 | 4 | 180 | veg |
Café Favourites | Protein Smoothie | 229 | 18 | 220 | veg |
Café Favourites | Fruits, Nuts & Seeds Bowl | 249 | 7 | 260 | veg |
Non-Veg Snacks | Chicken Pepper Bites | 279 | 22 | 210 | non_veg |
Non-Veg Snacks | Fish Crispy Bites | 299 | 20 | 200 | non_veg |
Non-Veg Snacks | Prawn Masala Bites | 329 | 21 | 190 | non_veg |
Non-Veg Snacks | Egg Masala Crunch | 229 | 14 | 180 | egg |
Non-Veg Snacks | Chicken Seekh Kebab Bites | 299 | 23 | 220 | non_veg |
Energy Bites & Cookies | Date & Chocolate Protein Bites | 249 | 8 | 140 | veg |
Energy Bites & Cookies | Mango Coconut Fruit Bites | 229 | 2 | 120 | veg |
Energy Bites & Cookies | Millet Chocolate Bites | 239 | 4 | 130 | veg |
Energy Bites & Cookies | Oats & Millet Cookies | 219 | 4 | 150 | veg |
Energy Bites & Cookies | Peanut Sesame Jaggery Bites | 199 | 6 | 160 | veg |
Energy Bites & Cookies | Ragi Cocoa Cookies | 219 | 4 | 145 | veg |
Wellness Shots | Morning Boost Shot | 99 | | 25 | veg | 30 ml
Wellness Shots | Night Reset Shot | 99 | | 20 | veg | 30 ml
Wellness Shots | Immunity Shield Shot | 129 | | 20 | veg | 30 ml
Wellness Shots | Digest Ease Shot | 99 | | 15 | veg | 30 ml
Wellness Shots | Breath Fresh Shot | 99 | | 15 | veg | 30 ml
Protein Bowls | Garlic Steak & Prawns Bowl | 449 | 38 | 420 | non_veg |
Protein Bowls | Grilled Chicken Quinoa Bowl | 379 | 35 | 400 | non_veg |
Protein Bowls | Lemon Herb Salmon Bowl | 469 | 32 | 380 | non_veg |
Protein Bowls | High-Protein Paneer Bowl | 329 | 26 | 360 | veg |
Protein Bowls | Egg & Avocado Power Bowl | 299 | 24 | 340 | egg |
"""

EXPECTED_GROUPS = {
    "fresh": (
        "Fresh & Ready to Eat",
        ["Café Favourites", "Protein Bowls", "Non-Veg Snacks", "Wellness Shots"],
    ),
    "packaged": (
        "Packaged Foods",
        [
            "Breakfast & Grains",
            "Protein & Active Nutrition",
            "Nuts, Seeds & Snacks",
            "Cooking Essentials",
            "Teas & Beverages",
            "Ready Mixes",
            "Energy Bites & Cookies",
        ],
    ),
}


def _spec_rows():
    rows = {}
    for line in SPEC_TABLE.strip().splitlines():
        cat, name, price, protein, kcal, diet, pack = (p.strip() for p in line.split("|"))
        rows[name] = {
            "category": cat,
            "price": Decimal(price),
            "protein": Decimal(protein) if protein else None,
            "kcal": int(kcal),
            "diet": diet,
            "pack": pack or None,
        }
    return rows


SPEC = _spec_rows()


@pytest.fixture(scope="module")
def data():
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def category_names(data):
    return {c["id"]: c["name"] for c in data["categories"]}


def test_header_note(data):
    assert data["_note"] == (
        "Development fixture. Prices and nutrition unverified. Not for production."
    )


def test_counts(data):
    assert len(data["groups"]) == 2
    assert len(data["categories"]) == 11
    assert len(data["products"]) == 55
    assert len({p["id"] for p in data["products"]}) == 55


def test_groups_and_their_categories(data):
    groups = {g["id"]: g for g in data["groups"]}
    assert set(groups) == set(EXPECTED_GROUPS)
    for gid, (name, cats) in EXPECTED_GROUPS.items():
        assert groups[gid]["name"] == name
        children = sorted(
            (c for c in data["categories"] if c["parent_id"] == gid),
            key=lambda c: c["sort_order"],
        )
        assert [c["name"] for c in children] == cats


def test_per_category_counts(data, category_names):
    actual = Counter(category_names[p["category_id"]] for p in data["products"])
    expected = Counter(row["category"] for row in SPEC.values())
    assert actual == expected
    assert expected["Nuts, Seeds & Snacks"] == 10


def test_every_product_matches_spec_row(data, category_names):
    assert {p["name"] for p in data["products"]} == set(SPEC)
    for p in data["products"]:
        row = SPEC[p["name"]]
        assert category_names[p["category_id"]] == row["category"], p["name"]
        assert isinstance(p["price"], str), "prices are strings so they load as Decimal"
        assert Decimal(p["price"]) == row["price"], p["name"]
        protein = None if p["protein_g"] is None else Decimal(p["protein_g"])
        assert protein == row["protein"], p["name"]
        assert p["calories_kcal"] == row["kcal"], p["name"]
        assert p["diet_type"] == row["diet"], p["name"]
        assert p["pack_size"] == row["pack"], p["name"]


def test_product_defaults(data):
    for p in data["products"]:
        assert p["currency"] == "INR"
        assert p["in_stock"] is True
        assert p["nutrition_verified"] is False
        assert len(p["variants"]) == 1
        (variant,) = p["variants"]
        assert variant["price"] == p["price"]
        assert variant["currency"] == "INR"
        assert variant["in_stock"] is True


def test_tag_rules(data):
    for p in data["products"]:
        row = SPEC[p["name"]]
        expected = set()
        if row["protein"] is not None and row["protein"] >= 20:
            expected.add("high_protein")
        if row["kcal"] <= 250:
            expected.add("light")
        assert set(p["tags"]) == expected, p["name"]


def test_egg_products(data):
    egg = {p["name"] for p in data["products"] if p["diet_type"] == "egg"}
    assert egg == {"Egg Masala Crunch", "Egg & Avocado Power Bowl"}


def test_blank_protein_is_null(data):
    blanks = {p["name"] for p in data["products"] if p["protein_g"] is None}
    assert blanks == {name for name, row in SPEC.items() if row["protein"] is None}
    assert len(blanks) == 8

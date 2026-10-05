"""Transcripts: add, update, remove; quantity limits come from the port."""

import re
from pathlib import Path

import pytest

from .harness import LANGUAGES, PHRASES, WA_ID, in_script, onboard


def cart_of(backend):
    customer = backend.get_or_create_customer(WA_ID)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_add_update_remove(chat, backend, lang):
    onboard(chat, lang=lang)
    chat.send(PHRASES[lang]["add"])
    assert chat.keys == ["cart.added", "cart.title", "cart.subtotal"]
    assert cart_of(backend) == {"Makhana Crunch": 2}
    assert "₹398" in chat.body  # 2 × ₹199, computed by the port
    assert in_script(chat.message("cart.subtotal").body.replace("₹398", ""), lang)

    chat.send(PHRASES[lang]["update"])
    assert chat.keys[0] == "cart.updated"
    assert cart_of(backend) == {"Makhana Crunch": 3}

    chat.send(PHRASES[lang]["cart"])
    assert chat.keys == ["cart.title", "cart.subtotal"]
    assert "₹597" in chat.body

    chat.send(PHRASES[lang]["remove"])
    assert chat.keys == ["cart.removed", "cart.empty"]
    assert cart_of(backend) == {}


def test_add_by_tapping_after_viewing_a_product(chat, backend):
    onboard(chat)
    chat.send("add makhana")
    chat.tap("ADD_TO_CART:makhana-crunch--std:3")
    assert chat.keys[0] == "cart.added"
    assert cart_of(backend) == {"Makhana Crunch": 3}


def test_typed_quantity_completes_the_pending_add(chat, backend):
    onboard(chat)
    chat.send("add makhana")
    chat.send("4 packets")
    assert cart_of(backend) == {"Makhana Crunch": 4}


def test_ambiguous_add_keeps_the_quantity(chat, backend):
    onboard(chat)
    chat.send("add 2 oats")
    assert chat.keys == ["choose.product"]
    chat.tap("PRODUCT_SELECT:ivar-oats")
    assert chat.keys[0] == "cart.added"
    assert cart_of(backend) == {"Ivar Oats": 2}


def test_adding_the_same_product_increases_the_line(chat, backend):
    onboard(chat)
    chat.send("add 2 makhana").send("add 1 makhana")
    assert cart_of(backend) == {"Makhana Crunch": 3}


def test_quantity_limit_comes_from_the_port(chat, backend):
    onboard(chat)
    chat.send("add 25 makhana")  # the Fake Backend allows 1..20; the engine does not know that
    assert chat.keys == ["qty.invalid"]
    assert cart_of(backend) == {}


def test_invalid_quantity_keeps_waiting_for_a_number(chat, backend):
    onboard(chat)
    chat.send("add makhana")
    chat.send("0")
    assert chat.keys == ["qty.invalid"]
    chat.send("2")
    assert cart_of(backend) == {"Makhana Crunch": 2}


def test_engine_never_hardcodes_the_quantity_limit():
    source = (Path(__file__).resolve().parents[1] / "engine.py").read_text(encoding="utf-8")
    assert "MAX_LINE_QUANTITY" not in source
    assert re.search(r"(?<!\d)20(?!\d)", source) is None


def test_edit_cart_by_tapping(chat, backend):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.tap("ACT:edit")
    assert chat.keys == ["cart.edit"]
    line_row = chat.ids[0]
    chat.tap(line_row)
    assert chat.keys == ["cart.line_actions"]
    update_id, remove_id, _ = chat.ids
    chat.tap(update_id)
    assert chat.keys == ["qty.ask_update"]
    chat.send("5")
    assert cart_of(backend) == {"Makhana Crunch": 5}
    chat.tap(remove_id)
    assert cart_of(backend) == {}


def test_remove_ambiguous_line_offers_a_choice(chat, backend):
    onboard(chat)
    chat.send("add 1 ivar oats").send("add 1 classic oats bowl")
    chat.send("remove oats")
    assert chat.keys == ["cart.choose_line"]
    assert len(chat.ids) == 2
    chat.tap(chat.ids[0])
    assert chat.keys[0] == "cart.removed"
    assert len(cart_of(backend)) == 1


def test_remove_something_not_in_the_cart(chat):
    onboard(chat)
    chat.send("add 1 makhana")
    chat.send("remove quinoa")
    assert chat.keys[0] == "cart.not_found"


def test_view_empty_cart(chat):
    onboard(chat)
    chat.send("show my cart")
    assert chat.keys == ["cart.empty"]

"""Transcripts: native WhatsApp catalog orders (simulated payloads, no live Meta)."""

import logging

import pytest

from apps.conversations.channel import Inbound
from apps.conversations.messages import t

from .harness import ADDRESS, WA_ID, in_script, onboard

OATS = "ivar-oats"  # 249.00 INR in the fixture
MIX = "ragi-mix"  # 299.00 INR


def item(product_id, quantity=1, price=None, currency="INR"):
    return {"product_id": product_id, "quantity": quantity, "price": price, "currency": currency}


def order(*items, catalog_id="catalog-1", unreadable=0):
    return {"catalog_id": catalog_id, "text": None, "items": list(items), "unreadable": unreadable}


def send_order(chat, *items, **kwargs):
    chat.last = chat.engine.handle(chat.whatsapp_id, Inbound(order=order(*items, **kwargs)))
    return chat


def cart_of(chat, backend):
    customer = backend.get_or_create_customer(WA_ID)
    return {ln.product_id: ln.quantity for ln in backend.get_cart(customer.id).lines}


def test_valid_order_replaces_cart_and_shows_backend_prices(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 2, 1.0), item(MIX, 1, 1.0))  # message prices are ignored
    assert chat.keys[0] == "order.cart_replaced"
    assert "cart.title" in chat.keys and "cart.subtotal" in chat.keys
    assert cart_of(chat, backend) == {OATS: 2, MIX: 1}
    assert "₹498" in chat.body and "₹299" in chat.body and "₹797" in chat.body
    assert "ACT:edit" in chat.ids


def test_cart_is_replaced_not_merged(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 3), item(MIX, 1))
    send_order(chat, item(OATS, 1))
    assert cart_of(chat, backend) == {OATS: 1}


def test_flow_continues_into_address_collection(chat):
    onboard(chat)
    send_order(chat, item(OATS, 1))
    chat.tap("CHECKOUT")
    assert chat.keys == ["checkout.type_address"]
    chat.send(ADDRESS)
    assert "checkout.confirm" in chat.keys


def test_unknown_product_is_excluded_and_counted_never_echoed(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 1), item("no-such-slug", 1))
    assert cart_of(chat, backend) == {OATS: 1}
    assert "order.excluded_unknown" in chat.keys
    body = chat.message("order.excluded_unknown").body
    assert "1 item(s)" in body and "no-such-slug" not in body  # a count, never a raw ID


def test_out_of_stock_item_is_excluded_and_named(chat, backend):
    onboard(chat)
    backend.set_stock(MIX, False)
    send_order(chat, item(OATS, 1), item(MIX, 1))
    assert cart_of(chat, backend) == {OATS: 1}
    assert "Ragi Mix" in chat.message("order.excluded_unavailable").body


def test_price_mismatch_uses_backend_price_and_tells_customer(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 1, 99))
    assert "order.price_changed" in chat.keys
    assert "₹249" in chat.body and "99" not in chat.message("cart.title").body


def test_matching_price_gives_no_price_notice(chat):
    onboard(chat)
    send_order(chat, item(OATS, 1, 249.0))
    assert "order.price_changed" not in chat.keys
    send_order(chat, item(OATS, 1, "249.00"))
    assert "order.price_changed" not in chat.keys


@pytest.mark.parametrize("bad", [0, -2, 1.5, "abc", None, True, "", [1]])
def test_invalid_quantity_is_excluded(chat, backend, bad):
    onboard(chat)
    send_order(chat, item(OATS, 1), item(MIX, bad))
    assert cart_of(chat, backend) == {OATS: 1}
    body = chat.message("order.excluded_quantity").body
    assert "1 item(s)" in body and MIX not in body  # a count, never a raw ID


def test_whole_number_quantities_in_other_encodings_are_accepted(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, "3"), item(MIX, 2.0))
    assert cart_of(chat, backend) == {OATS: 3, MIX: 2}


def test_quantity_above_cap_is_clamped_and_reported(chat, backend, settings):
    settings.IVA_CATALOG_ORDER_MAX_QTY = 5
    onboard(chat)
    send_order(chat, item(OATS, 9))
    assert cart_of(chat, backend) == {OATS: 5}
    assert "5" in chat.message("order.clamped").body and "Ivar Oats" in chat.body


def test_cap_never_exceeds_the_port_limit(chat, backend, settings):
    settings.IVA_CATALOG_ORDER_MAX_QTY = 500
    onboard(chat)
    send_order(chat, item(OATS, 400))
    assert cart_of(chat, backend) == {OATS: 20}


def test_duplicate_lines_are_merged(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 2), item(OATS, 3))
    assert cart_of(chat, backend) == {OATS: 5}


def test_all_items_excluded_leaves_cart_untouched_and_offers_menu(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 2))
    before = cart_of(chat, backend)
    send_order(chat, item("no-such-slug", 1), item(MIX, 0))
    assert cart_of(chat, backend) == before == {OATS: 2}
    assert chat.keys[-1] == "order.none_added"
    assert chat.ids == ["CATEGORY_SELECT"]


def test_catalog_mismatch_is_ignored(chat, backend, settings):
    settings.IVA_CATALOG_ID = "catalog-1"
    onboard(chat)
    send_order(chat, item(OATS, 1), catalog_id="someone-elses")
    assert chat.keys == ["order.wrong_catalog"]
    assert cart_of(chat, backend) == {}
    send_order(chat, item(OATS, 1), catalog_id="catalog-1")
    assert cart_of(chat, backend) == {OATS: 1}


def test_catalog_check_is_skipped_when_unset(chat, backend, settings):
    settings.IVA_CATALOG_ID = ""
    onboard(chat)
    send_order(chat, item(OATS, 1), catalog_id="anything")
    assert cart_of(chat, backend) == {OATS: 1}


def test_malformed_order_gets_a_safe_reply(chat, backend):
    onboard(chat)
    send_order(chat)
    assert chat.keys == ["order.unreadable"]
    send_order(chat, unreadable=3)
    assert chat.keys == ["order.unreadable"]
    assert cart_of(chat, backend) == {}


def test_unreadable_items_are_counted(chat, backend):
    onboard(chat)
    send_order(chat, item(OATS, 1), unreadable=2)
    assert cart_of(chat, backend) == {OATS: 1}
    assert "2" in chat.message("order.excluded_unreadable").body


def test_unsafe_product_ids_are_not_echoed(chat):
    onboard(chat)
    send_order(chat, item("<script>alert(1)</script>", 1), item(OATS, 1))
    assert "script" not in chat.body


@pytest.mark.parametrize("lang", ["te", "hi"])
def test_reply_uses_the_customers_language(chat, lang):
    onboard(chat, lang=lang)
    send_order(chat, item(OATS, 1), item("no-such-slug", 1))
    assert chat.message("order.cart_replaced").body == t(lang, "order.cart_replaced")
    assert in_script(chat.message("order.excluded_unknown").body, lang)


def test_order_before_onboarding_asks_to_finish_setup(chat, backend):
    send_order(chat, item(OATS, 1))
    assert chat.keys[0] == "order.setup_first"
    assert cart_of(chat, backend) == {}


def test_logs_ids_and_counts_only(chat, caplog):
    onboard(chat)
    with caplog.at_level(logging.INFO):
        send_order(chat, item(OATS, 1), item("no-such-slug", 1))
    text = " ".join(r.getMessage() for r in caplog.records if "catalog order" in r.getMessage())
    assert OATS in text and "no-such-slug" in text and "accepted=1" in text
    assert WA_ID not in caplog.text

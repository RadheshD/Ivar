"""Validation of a native WhatsApp catalog order against the Backend Port.

The customer's order message says *which* products and how many. Everything else (does
the product exist, is it in stock, what does it cost) comes from the port. The
``item_price``/``currency`` in the message are never used as a price; they are only
compared with the backend so the customer can be told when a price differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .backend_port import MAX_LINE_QUANTITY, BackendPort, NotFound, ProductUnavailable

# The port's per-line maximum, under a name the engine may import (the engine itself must
# never reference the limit directly; see test_engine_never_hardcodes_the_quantity_limit).
PORT_MAX_QUANTITY = MAX_LINE_QUANTITY

# Product IDs echoed back to a customer (unknown items) must look like real slugs.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass
class OrderLine:
    product_id: str
    name: str
    variant_id: str
    quantity: int


@dataclass
class ValidatedOrder:
    lines: list[OrderLine] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # product IDs (safe form) not in the catalog
    unreadable: int = 0  # items that could not be identified or whose ID is unsafe to echo
    unavailable: list[str] = field(default_factory=list)  # product names
    bad_quantity: list[str] = field(default_factory=list)  # product names or IDs
    clamped: list[str] = field(default_factory=list)  # product names cut to the cap
    price_changed: list[str] = field(default_factory=list)  # product names
    cap: int = MAX_LINE_QUANTITY


def effective_cap(configured: int) -> int:
    """The per-item cap: the setting, never above the port's limit, never below 1."""
    return max(1, min(int(configured), MAX_LINE_QUANTITY))


def parse_quantity(raw) -> int | None:
    """A positive whole number, or ``None``. Booleans, fractions, zero and negatives fail."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw.isascii() or not raw.isdigit():
            return None
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        value = int(raw)
    else:
        return None
    return value if value > 0 else None


def _price_differs(raw_price, raw_currency, price: Decimal, currency: str) -> bool:
    if raw_currency is not None and str(raw_currency).upper() != currency.upper():
        return True
    if raw_price is None or isinstance(raw_price, bool):
        return False  # nothing to compare; the backend price is used regardless
    try:
        sent = Decimal(str(raw_price).strip())
    except InvalidOperation:
        return False
    return sent != price


def validate_order(backend: BackendPort, items: list[dict], cap: int) -> ValidatedOrder:
    """Check each item through the port. Never raises for bad input; a backend outage
    (``BackendUnavailable``) propagates so the caller can ask the customer to retry."""
    result = ValidatedOrder(cap=cap)
    # product_id -> [total quantity, sent price/currency pairs]
    merged: dict[str, list] = {}
    for item in items:
        product_id = item.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            result.unreadable += 1
            continue
        quantity = parse_quantity(item.get("quantity"))
        if quantity is None:
            label = product_id if SAFE_ID_RE.match(product_id) else None
            if label is None:
                result.unreadable += 1
            else:
                result.bad_quantity.append(label)
            continue
        entry = merged.setdefault(product_id, [0, []])
        entry[0] += quantity
        entry[1].append((item.get("price"), item.get("currency")))

    for product_id, (quantity, sent_prices) in merged.items():
        try:
            product = backend.get_product(product_id)
        except NotFound:
            if SAFE_ID_RE.match(product_id):
                result.unknown.append(product_id)
            else:
                result.unreadable += 1
            continue
        except ProductUnavailable:
            result.unavailable.append(product_id)
            continue
        in_stock = [v for v in product.variants if v.in_stock]
        if not product.in_stock or not in_stock:
            result.unavailable.append(product.name)
            continue
        variant = in_stock[0]
        if any(_price_differs(p, c, variant.price, variant.currency) for p, c in sent_prices):
            result.price_changed.append(product.name)
        if quantity > cap:
            quantity = cap
            result.clamped.append(product.name)
        result.lines.append(OrderLine(product_id, product.name, variant.id, quantity))
    return result

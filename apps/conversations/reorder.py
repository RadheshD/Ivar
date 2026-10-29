"""Planning a reorder: which lines of a past order can go into the cart now, and what differs.

Everything comes from the Backend Port: the order's stored lines (``get_order_detail``) and
today's product, variant, stock and price (``get_product``). A line is available only if the
SAME variant is in stock; a variant that is gone is never replaced by another one. Prices in
the plan are the CURRENT prices; the order-time price is kept only to tell the customer what
changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from apps.iva_common.money import quantize

from .backend_port import BackendPort, NotFound, OrderDetail, ProductUnavailable
from .catalog_order import PORT_MAX_QUANTITY


@dataclass(frozen=True)
class PlannedLine:
    product_id: str
    variant_id: str
    name: str
    quantity: int
    unit_price: Decimal  # current
    old_price: Decimal  # as at the time of the order
    currency: str
    currency_changed: bool = False

    @property
    def price_changed(self) -> bool:
        return self.currency_changed or self.unit_price != self.old_price

    @property
    def total(self) -> Decimal:
        return quantize(self.unit_price * self.quantity)


@dataclass
class ReorderPlan:
    order_number: str
    currency: str
    lines: list[PlannedLine] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # names
    clamped: list[str] = field(default_factory=list)  # names cut to the cart limit
    cap: int = PORT_MAX_QUANTITY

    @property
    def price_changes(self) -> list[PlannedLine]:
        return [line for line in self.lines if line.price_changed]

    @property
    def differs(self) -> bool:
        """True when the customer must be shown something before anything is added."""
        return bool(self.unavailable or self.clamped or self.price_changes)

    @property
    def total(self) -> Decimal:
        return quantize(sum((line.total for line in self.lines), Decimal("0")))

    def fingerprint(self, cart_lines: Iterable[tuple[str, int]]) -> str:
        """A short digest of the plan AND the cart it would go into. It rides in the preview's
        buttons, so a tap made after anything changed (stock, price, or the cart itself,
        for example by an earlier tap on the same button) is recognised without any session."""
        parts = (
            tuple((ln.variant_id, ln.quantity, str(ln.unit_price)) for ln in self.lines),
            tuple(sorted(self.unavailable)),
            tuple(sorted(self.clamped)),
            tuple(sorted(cart_lines)),
        )
        return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:10]


def plan_reorder(backend: BackendPort, detail: OrderDetail) -> ReorderPlan:
    """Check every stored line against the catalog as it is now. Never raises for a product
    that is gone or out of stock; a backend outage (``BackendUnavailable``) propagates."""
    plan = ReorderPlan(order_number=detail.order_number, currency=detail.currency)
    for line in detail.lines:
        try:
            product = backend.get_product(line.product_id)
        except (NotFound, ProductUnavailable):
            plan.unavailable.append(line.name)
            continue
        variant = next((v for v in product.variants if v.id == line.variant_id), None)
        if variant is None or not product.in_stock or not variant.in_stock:
            plan.unavailable.append(line.name)  # never swapped for another variant
            continue
        quantity = line.quantity
        if quantity > plan.cap:
            quantity = plan.cap
            plan.clamped.append(product.name)
        plan.lines.append(
            PlannedLine(
                product_id=product.id,
                variant_id=variant.id,
                name=product.name,
                quantity=quantity,
                unit_price=quantize(variant.price),
                old_price=quantize(line.unit_price),
                currency=variant.currency,
                currency_changed=variant.currency != detail.currency,
            )
        )
    return plan

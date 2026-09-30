"""Backend Port: the only contract between the WhatsApp channel and the Ivar backend.

This file is shared with the organization's backend team. Their adapter implements
every protocol below; the channel never touches backend models or tables.

Conventions
-----------
* All IDs are strings. Money is ``Decimal`` plus an ISO currency code, never float.
* Datetimes are timezone-aware.
* ``ProductSummary.image_url`` (and ``ProductDetail.image_url``) is either an ``https://``
  URL or ``None``. ``None`` means no image; the channel must render a text-only card, never
  guess or construct a URL itself.
* Every value about price, stock, payment, order status, ETA or tracking comes
  from the backend. The channel only displays what these methods return.
* Failures are reported with the typed errors at the bottom of this file. Invalid
  arguments that are programming errors (e.g. an unknown diet code) raise
  ``ValueError``.

Semantics that the contract tests (``tests/contract/``) enforce
---------------------------------------------------------------
* ``list_categories(None)`` returns top-level groups; with a ``parent_id`` it
  returns that group's children. Unknown ``parent_id`` -> ``NotFound``.
* ``list_products(diet=...)`` filters by the customer's dietary *preference*:
  ``veg`` -> veg only, ``egg`` -> veg + egg, ``non_veg`` -> everything.
  ``category_id`` may name a group (all its categories) or a single category.
  Out-of-stock products are listed with ``in_stock=False``. ``limit`` is 1..50.
* ``add_item`` for a variant already in the cart increases that line's quantity.
  Quantities are integers from 1 to ``MAX_LINE_QUANTITY``; to delete a line use
  ``remove_item``.
* A cart line, address or order owned by another customer -> ``NotAuthorized``.
  An order number that does not exist -> ``None`` from ``get_order``.
* ``checkout`` validates the cart, creates an order with ``order_status=PENDING``
  and ``payment_status=PENDING``, empties the cart and returns a payment URL.
  The same ``(customer_id, idempotency_key)`` returns the same result without
  creating a second order. Only a verified payment-provider confirmation may
  make payment ``PAID`` and then the order ``CONFIRMED``.
* ``OrderStatusView.events`` are oldest first. Order-status events use the
  ``OrderStatus`` values; payment events use ``PAYMENT_<PaymentStatus>``
  (e.g. ``PAYMENT_PAID``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

MAX_LINE_QUANTITY = 20
MAX_PAGE_SIZE = 50

LANGUAGES = ("en", "te", "hi")
DIETS = ("veg", "egg", "non_veg")


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    PACKED = "PACKED"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


def payment_event(status: PaymentStatus | str) -> str:
    """Event status string used for a payment-status change, e.g. ``PAYMENT_PAID``."""
    return f"PAYMENT_{PaymentStatus(status).value}"


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class CustomerRef:
    id: str
    whatsapp_id: str
    name: str | None
    city: str | None
    language: str | None  # "en" | "te" | "hi"
    diet: str | None  # "veg" | "egg" | "non_veg"
    onboarding_complete: bool


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    parent_id: str | None
    sort_order: int


@dataclass(frozen=True)
class ProductSummary:
    id: str
    name: str
    category_id: str
    diet_type: str
    price: Decimal
    currency: str
    in_stock: bool
    protein_g: Decimal | None
    calories_kcal: int | None
    tags: tuple[str, ...]
    pack_size: str | None
    image_url: str | None  # https URL or None; never a bare path, never guessed


@dataclass(frozen=True)
class Variant:
    id: str
    label: str
    price: Decimal
    currency: str
    in_stock: bool


@dataclass(frozen=True)
class ProductDetail(ProductSummary):
    description: str | None
    variants: tuple[Variant, ...]
    nutrition_verified: bool


@dataclass(frozen=True)
class CartLine:
    line_id: str
    product_id: str
    variant_id: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class CartView:
    lines: tuple[CartLine, ...]
    subtotal: Decimal
    currency: str


@dataclass(frozen=True)
class Address:
    id: str
    label: str | None
    text: str


@dataclass(frozen=True)
class CheckoutResult:
    order_number: str
    order_status: str
    payment_status: str
    total: Decimal
    currency: str
    payment_url: str | None


@dataclass(frozen=True)
class OrderStatusEvent:
    status: str
    at: datetime


@dataclass(frozen=True)
class OrderStatusView:
    order_number: str
    order_status: str
    payment_status: str
    events: tuple[OrderStatusEvent, ...]
    eta_start: datetime | None
    eta_end: datetime | None
    tracking_id: str | None


# --------------------------------------------------------------------------- ports


@runtime_checkable
class CustomerPort(Protocol):
    def get_or_create_customer(self, whatsapp_id: str) -> CustomerRef: ...

    def update_profile(
        self,
        customer_id: str,
        *,
        name: str | None = None,
        city: str | None = None,
        language: str | None = None,
        diet: str | None = None,
        onboarding_complete: bool | None = None,
    ) -> CustomerRef: ...


@runtime_checkable
class CatalogPort(Protocol):
    def list_categories(self, parent_id: str | None = None) -> list[Category]: ...

    def list_products(
        self,
        *,
        category_id: str | None = None,
        search: str | None = None,
        diet: str | None = None,
        tag: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[ProductSummary], int]: ...

    def get_product(self, product_id: str) -> ProductDetail: ...

    def resolve_product_names(self, text: str, limit: int = 5) -> list[ProductSummary]: ...


@runtime_checkable
class CartPort(Protocol):
    def get_cart(self, customer_id: str) -> CartView: ...

    def add_item(self, customer_id: str, variant_id: str, quantity: int) -> CartView: ...

    def update_quantity(self, customer_id: str, line_id: str, quantity: int) -> CartView: ...

    def remove_item(self, customer_id: str, line_id: str) -> CartView: ...


@runtime_checkable
class AddressPort(Protocol):
    def list_addresses(self, customer_id: str) -> list[Address]: ...

    def add_address(self, customer_id: str, text: str, label: str | None = None) -> Address: ...


@runtime_checkable
class CheckoutPort(Protocol):
    def checkout(
        self, customer_id: str, address_id: str, idempotency_key: str
    ) -> CheckoutResult: ...


@runtime_checkable
class OrderPort(Protocol):
    def get_latest_order(self, customer_id: str) -> OrderStatusView | None: ...

    def get_order(self, customer_id: str, order_number: str) -> OrderStatusView | None: ...

    def list_recent_orders(self, customer_id: str, limit: int = 5) -> list[OrderStatusView]: ...


@runtime_checkable
class BackendPort(
    CustomerPort, CatalogPort, CartPort, AddressPort, CheckoutPort, OrderPort, Protocol
):
    """Everything an adapter must implement."""


PORTS: tuple[type, ...] = (
    CustomerPort,
    CatalogPort,
    CartPort,
    AddressPort,
    CheckoutPort,
    OrderPort,
)


def port_operations() -> tuple[str, ...]:
    """Names of every operation an adapter must implement, in declaration order."""
    names: list[str] = []
    for port in PORTS:
        names.extend(n for n, v in vars(port).items() if callable(v) and not n.startswith("_"))
    return tuple(names)


# --------------------------------------------------------------------------- errors


class BackendError(Exception):
    """Base class for every error an adapter may raise."""


class BackendUnavailable(BackendError):
    """Timeout, connection failure or 5xx from the backend."""


class NotFound(BackendError):
    """Product, variant, category, customer, address or cart line does not exist."""


class ProductUnavailable(BackendError):
    """Product is out of stock or inactive."""


class InvalidQuantity(BackendError):
    """Quantity is not an integer in 1..MAX_LINE_QUANTITY."""


class CartEmpty(BackendError):
    """Checkout attempted with an empty cart."""


class CheckoutRejected(BackendError):
    """Backend refused the checkout. ``reason`` is a safe, customer-agnostic code."""

    PRODUCT_UNAVAILABLE = "product_unavailable"
    PRICE_CHANGED = "price_changed"
    ADDRESS_NOT_SERVICEABLE = "address_not_serviceable"
    BELOW_MINIMUM_ORDER = "below_minimum_order"
    OTHER = "other"

    def __init__(self, reason: str = OTHER):
        super().__init__(reason)
        self.reason = reason


class NotAuthorized(BackendError):
    """The customer does not own the resource."""

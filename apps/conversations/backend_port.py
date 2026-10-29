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

Push events (backend -> channel; not a port operation)
------------------------------------------------------
The backend PUSHES ``OrderStatusChangeEvent`` messages to the channel when an order's
status changes; the channel never polls. See ``OrderStatusChangeEvent`` and
``parse_order_status_change`` below, and ARCHITECTURE.md ("Order status notifications").
(Not to be confused with ``OrderStatusEvent``, the history entry in
``OrderStatusView.events``.) Before pushing, the backend must already return the new status
from ``get_order``: the channel re-reads the order and does not notify for an event the
order no longer matches.
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


# Push contract: the backend tells the channel an order's status changed.
EVENT_ID_MAX = 128
_EVENT_FIELDS = ("event_id", "order_number", "customer_id", "new_status", "occurred_at")
_EVENT_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


class InvalidOrderEvent(ValueError):
    """A pushed event does not meet the contract. The message never echoes the input."""


@dataclass(frozen=True)
class OrderStatusChangeEvent:
    """``new_status`` is an ``OrderStatus`` value. ``event_id`` is unique per status change and
    is reused verbatim when the backend redelivers it (the channel notifies at most once per
    ``event_id``). ``customer_id`` is the backend's ID, never a phone number."""

    event_id: str
    order_number: str
    customer_id: str
    new_status: str
    occurred_at: datetime


def _event_token(data: dict, key: str, max_length: int) -> str:
    value = data[key]
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or not set(value) <= _EVENT_ID_CHARS
    ):
        raise InvalidOrderEvent(f"Invalid {key}.")
    return value


def parse_order_status_change(data: object) -> OrderStatusChangeEvent:
    """Validate a decoded JSON object strictly: exactly the five fields, each well formed,
    ``new_status`` a known ``OrderStatus``, ``occurred_at`` an ISO-8601 time WITH a zone.
    Raises ``InvalidOrderEvent`` (never anything else, never with the input in the text)."""
    if not isinstance(data, dict) or set(data) != set(_EVENT_FIELDS):
        raise InvalidOrderEvent("Wrong fields.")
    event_id = _event_token(data, "event_id", EVENT_ID_MAX)
    order_number = _event_token(data, "order_number", 64)
    customer_id = _event_token(data, "customer_id", 64)
    status = data["new_status"]
    if not isinstance(status, str) or status not in {s.value for s in OrderStatus}:
        raise InvalidOrderEvent("Invalid new_status.")
    raw_time = data["occurred_at"]
    try:
        occurred_at = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
    except ValueError:
        occurred_at = None
    if occurred_at is None or occurred_at.tzinfo is None:
        raise InvalidOrderEvent("Invalid occurred_at.")
    return OrderStatusChangeEvent(event_id, order_number, customer_id, status, occurred_at)


@dataclass(frozen=True)
class OrderStatusView:
    order_number: str
    order_status: str
    payment_status: str
    events: tuple[OrderStatusEvent, ...]
    eta_start: datetime | None
    eta_end: datetime | None
    tracking_id: str | None
    # Optional: an adapter that does not know them leaves them ``None``.
    placed_at: datetime | None = None
    total: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class OrderLine:
    """One line of a placed order. Name and prices are as at the time of the order."""

    product_id: str
    variant_id: str
    name: str
    variant_label: str | None
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class OrderDetail:
    order_number: str
    placed_at: datetime
    total: Decimal
    currency: str
    order_status: str
    payment_status: str
    lines: tuple[OrderLine, ...]


# --------------------------------------------------------------------------- ports


@runtime_checkable
class CustomerPort(Protocol):
    def get_or_create_customer(self, whatsapp_id: str) -> CustomerRef: ...

    def get_customer(self, customer_id: str) -> CustomerRef | None: ...

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

    def get_order_detail(self, customer_id: str, order_number: str) -> OrderDetail | None: ...


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

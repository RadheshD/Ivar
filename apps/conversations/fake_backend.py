"""In-memory, deterministic Backend Port implementation.

FOR DEVELOPMENT AND TESTS ONLY. Production settings refuse to start with it.
Data comes from ``fixtures/ivar_catalog.json`` (unverified prices and nutrition).
Every call is logged with ``backend=fake`` so fake data is always identifiable.

``image_url`` is built from ``IVA_PRODUCT_IMAGE_BASE_URL`` (settings, or the
``image_base_url`` constructor argument) plus the fixture's relative ``image_path``,
never hardcoded. Unset base URL, non-https base URL, or a null ``image_path`` all mean
``image_url=None``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from apps.iva_common.money import quantize, to_decimal

from .backend_port import (
    DIETS,
    LANGUAGES,
    MAX_LINE_QUANTITY,
    MAX_PAGE_SIZE,
    Address,
    CartEmpty,
    CartLine,
    CartView,
    Category,
    CheckoutRejected,
    CheckoutResult,
    CustomerRef,
    InvalidQuantity,
    NotAuthorized,
    NotFound,
    OrderStatus,
    OrderStatusEvent,
    OrderStatusView,
    PaymentStatus,
    ProductDetail,
    ProductSummary,
    ProductUnavailable,
    Variant,
    payment_event,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ivar_catalog.json"
FAKE_PAYMENT_URL = "https://example.invalid/pay/{order_number}"
FIRST_ORDER_NUMBER = 1001
CURRENCY = "INR"

# Which product diets a customer with a given preference may see.
_DIET_ALLOWS = {
    "veg": {"veg"},
    "egg": {"veg", "egg"},
    "non_veg": {"veg", "egg", "non_veg"},
}

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _https_or_empty(url: str) -> str:
    """``url`` unchanged if it is a real https URL, else ``""`` (never guessed)."""
    parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else ""


def _image_base_url_from_settings() -> str:
    """``IVA_PRODUCT_IMAGE_BASE_URL``, or ``""`` if unset or not https."""
    from django.conf import settings

    return _https_or_empty(getattr(settings, "IVA_PRODUCT_IMAGE_BASE_URL", "") or "")


def _state_file_from_settings() -> str:
    """``IVA_FAKE_BACKEND_STATE_FILE`` (dev only), or ``""`` for in-memory only."""
    from django.conf import settings

    return getattr(settings, "IVA_FAKE_BACKEND_STATE_FILE", "") or ""


_REVISION_RE = re.compile(rb'"revision"\s*:\s*(\d+)')


def _file_revision(path: Path | str) -> int:
    """The save counter stored in a state file (0 if the file is missing, unreadable or
    from before the counter existed). Only the first bytes are read: the counter is the
    first key of the saved ``seq`` object."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return 0
    match = _REVISION_RE.search(head)
    return int(match.group(1)) if match else 0


def state_file_counts(path: Path | str) -> dict[str, int]:
    """Plain counts of what a state file holds (never any values); zeros if unreadable."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "customers": len(data["customers"]),
            "cart lines": sum(len(v) for v in data["carts"].values()),
            "addresses": len(data["addresses"]),
            "orders": len(data["orders"]),
        }
    except Exception:
        return {"customers": 0, "cart lines": 0, "addresses": 0, "orders": 0}


def clear_state_file(path: Path | str) -> None:
    """Replace the file's contents with an empty state (a running dev server notices)."""
    empty = FakeBackend(state_file="")
    target = Path(path)
    empty._state_file = target
    empty._save_state_file()


@dataclass
class _Line:
    line_id: str
    variant_id: str
    quantity: int


@dataclass
class _Order:
    order_number: str
    customer_id: str
    address_id: str
    total: Decimal
    currency: str
    order_status: str
    payment_status: str
    payment_url: str
    events: list[OrderStatusEvent] = field(default_factory=list)
    eta_start: datetime | None = None
    eta_end: datetime | None = None
    tracking_id: str | None = None


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def _check_quantity(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise InvalidQuantity("Quantity must be an integer.")
    if not 1 <= quantity <= MAX_LINE_QUANTITY:
        raise InvalidQuantity(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")
    return quantity


class FakeBackend:
    """Implements every Backend Port operation in memory."""

    def __init__(
        self,
        fixture_path: Path | str = FIXTURE_PATH,
        clock: Callable[[], datetime] | None = None,
        image_base_url: str | None = None,
        state_file: Path | str | None = None,
    ):
        self._fixture_path = Path(fixture_path)
        self._clock = clock or (lambda: datetime.now(UTC))
        # None (the default): read from settings. An explicit value (including "") is
        # still re-checked for https, so a misconfigured caller cannot smuggle in a
        # non-https base URL.
        self._image_base_url = (
            _image_base_url_from_settings()
            if image_base_url is None
            else _https_or_empty(image_base_url)
        )
        # Dev-only persistence of customer state. None (the default): read from settings.
        # "" or unset: in-memory only (tests, production).
        if state_file is None:
            state_file = _state_file_from_settings()
        self._state_file = Path(state_file) if state_file else None
        self._file_sig: tuple[int, int, int] | None = None
        # Monotonic save counter, written into every saved state. Part of the change
        # signature, so a same-size rewrite inside one timestamp tick is still noticed.
        self._revision = 0
        self._dirty = False
        self._lock = threading.RLock()
        self._reset_state()
        self._load_state_file()

    # ------------------------------------------------------------------ persistence

    @contextlib.contextmanager
    def _txn(self):
        """Hold the lock for a customer-state operation. With a state file, first pick up
        changes another process (or ``reset_dev_data``) made to it, and save afterwards if
        the operation changed anything (``_changed``)."""
        with self._lock:
            self._refresh()
            try:
                yield
            finally:
                if self._dirty:
                    self._dirty = False
                    self._save_state_file()

    def _changed(self) -> None:
        self._dirty = True

    def _stat_sig(self) -> tuple[int, int, int] | None:
        """``(revision, mtime_ns, size)`` of the state file, or ``None`` if it is missing.
        The revision does not depend on the file system's timestamp resolution."""
        try:
            st = self._state_file.stat()
        except OSError:
            return None
        return (_file_revision(self._state_file), st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        if self._state_file is None or self._stat_sig() == self._file_sig:
            return
        self._reset_state()
        self._load_state_file()

    def _load_state_file(self) -> None:
        if self._state_file is None:
            return
        self._file_sig = self._stat_sig()
        if self._file_sig is None:  # no file yet: start empty
            return
        try:
            with self._state_file.open(encoding="utf-8") as fh:
                self._restore(json.load(fh))
        except Exception as exc:
            logger.warning(
                "fake backend: state file unreadable (%s); starting with empty state",
                type(exc).__name__,
            )
            self._reset_state()
            with contextlib.suppress(OSError):  # keep the unreadable file for inspection
                os.replace(self._state_file, self._state_file.with_name(
                    self._state_file.name + ".corrupt"
                ))  # fmt: skip
            self._file_sig = self._stat_sig()

    def _save_state_file(self) -> None:
        if self._state_file is None:
            return
        previous = self._revision
        # Never behind what is on disk, even if another process saved since we loaded.
        self._revision = max(self._revision, _file_revision(self._state_file)) + 1
        data = json.dumps(self._snapshot(), indent=1)
        tmp = self._state_file.with_name(self._state_file.name + ".tmp")
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            for attempt in range(5):  # Windows/OneDrive can briefly lock the target
                try:
                    os.replace(tmp, self._state_file)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            self._file_sig = self._stat_sig()
        except OSError as exc:
            self._revision = previous  # nothing was saved
            logger.error("fake backend: could not save state file (%s)", type(exc).__name__)

    def _snapshot(self) -> dict:
        def when(value):
            return value.isoformat() if value else None

        return {
            "version": 1,
            # "revision" is first on purpose: see _file_revision. It lives inside "seq"
            # (the counters) so the file's top-level shape is unchanged.
            "seq": {"revision": self._revision, "customer": self._customer_seq,
                    "line": self._line_seq, "address": self._address_seq},
            "customers": [asdict(c) for c in self._customers.values()],
            "carts": {
                cid: [asdict(line) for line in lines.values()]
                for cid, lines in self._carts.items()
            },
            "addresses": [
                {**asdict(address), "owner": owner} for address, owner in self._addresses.values()
            ],
            "orders": [
                {
                    "order_number": o.order_number,
                    "customer_id": o.customer_id,
                    "address_id": o.address_id,
                    "total": str(o.total),
                    "currency": o.currency,
                    "order_status": o.order_status,
                    "payment_status": o.payment_status,
                    "payment_url": o.payment_url,
                    "events": [{"status": e.status, "at": when(e.at)} for e in o.events],
                    "eta_start": when(o.eta_start),
                    "eta_end": when(o.eta_end),
                    "tracking_id": o.tracking_id,
                }
                for o in self._orders.values()
            ],
            "checkouts": [
                {
                    "customer_id": cid,
                    "key": key,
                    "result": {**asdict(r), "total": str(r.total)},
                }
                for (cid, key), r in self._checkouts.items()
            ],
        }  # fmt: skip

    def _restore(self, data: dict) -> None:
        def when(value):
            return datetime.fromisoformat(value) if value else None

        if data.get("version") != 1:
            raise ValueError("unknown state version")
        seq = data["seq"]
        revision = seq.get("revision", 0)  # files from before the counter: revision 0
        if isinstance(revision, int) and not isinstance(revision, bool) and revision > 0:
            self._revision = max(self._revision, revision)
        self._customer_seq, self._line_seq = int(seq["customer"]), int(seq["line"])
        self._address_seq = int(seq["address"])
        for c in data["customers"]:
            customer = CustomerRef(**c)
            self._customers[customer.id] = customer
            self._customer_by_wa[customer.whatsapp_id] = customer.id
        for cid, lines in data["carts"].items():
            self._carts[cid] = {
                ln["line_id"]: _Line(**ln)
                for ln in lines
                if ln["variant_id"] in self._variant_product
            }  # lines for products no longer in the catalog are dropped
        for a in data["addresses"]:
            owner = a.pop("owner")
            address = Address(**a)
            self._addresses[address.id] = (address, owner)
        for o in data["orders"]:
            order = _Order(
                order_number=o["order_number"],
                customer_id=o["customer_id"],
                address_id=o["address_id"],
                total=to_decimal(o["total"]),
                currency=o["currency"],
                order_status=o["order_status"],
                payment_status=o["payment_status"],
                payment_url=o["payment_url"],
                events=[OrderStatusEvent(e["status"], when(e["at"])) for e in o["events"]],
                eta_start=when(o["eta_start"]),
                eta_end=when(o["eta_end"]),
                tracking_id=o["tracking_id"],
            )
            self._orders[order.order_number] = order
        for item in data["checkouts"]:
            r = item["result"]
            self._checkouts[(item["customer_id"], item["key"])] = CheckoutResult(
                **{**r, "total": to_decimal(r["total"])}
            )

    # ------------------------------------------------------------------ helpers

    def _log(self, op: str, **fields: object) -> None:
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.info("backend=fake op=%s %s", op, extra)

    def _load_fixture(self) -> None:
        with self._fixture_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        self._categories: dict[str, Category] = {}
        for g in data["groups"]:
            self._categories[g["id"]] = Category(g["id"], g["name"], None, g["sort_order"])
        for c in data["categories"]:
            self._categories[c["id"]] = Category(
                c["id"], c["name"], c["parent_id"], c["sort_order"]
            )
        self._products: dict[str, ProductDetail] = {}
        self._variant_product: dict[str, str] = {}
        for p in data["products"]:
            image_path = p.get("image_path")
            image_url = (
                f"{self._image_base_url.rstrip('/')}/{image_path}"
                if self._image_base_url and image_path
                else None
            )
            variants = tuple(
                Variant(
                    id=v["id"],
                    label=v["label"],
                    price=to_decimal(v["price"]),
                    currency=v["currency"],
                    in_stock=bool(v["in_stock"]),
                )
                for v in p["variants"]
            )
            product = ProductDetail(
                id=p["id"],
                name=p["name"],
                category_id=p["category_id"],
                diet_type=p["diet_type"],
                price=to_decimal(p["price"]),
                currency=p["currency"],
                in_stock=bool(p["in_stock"]),
                protein_g=None if p["protein_g"] is None else to_decimal(p["protein_g"]),
                calories_kcal=p["calories_kcal"],
                tags=tuple(p["tags"]),
                pack_size=p["pack_size"],
                image_url=image_url,
                description=p["description"],
                variants=variants,
                nutrition_verified=bool(p["nutrition_verified"]),
            )
            self._products[product.id] = product
            for v in variants:
                self._variant_product[v.id] = product.id
        # Display order: group order, then category order, then fixture order.
        self._product_order = {pid: i for i, pid in enumerate(self._products)}

    def _customer(self, customer_id: str) -> CustomerRef:
        try:
            return self._customers[customer_id]
        except KeyError:
            raise NotFound("Customer not found.") from None

    @staticmethod
    def _summary(p: ProductDetail) -> ProductSummary:
        return ProductSummary(
            id=p.id,
            name=p.name,
            category_id=p.category_id,
            diet_type=p.diet_type,
            price=p.price,
            currency=p.currency,
            in_stock=p.in_stock,
            protein_g=p.protein_g,
            calories_kcal=p.calories_kcal,
            tags=p.tags,
            pack_size=p.pack_size,
            image_url=p.image_url,
        )

    def _sort_key(self, p: ProductDetail) -> tuple[int, int, int]:
        cat = self._categories[p.category_id]
        group = self._categories[cat.parent_id] if cat.parent_id else cat
        return (group.sort_order, cat.sort_order, self._product_order[p.id])

    def _variant(self, variant_id: str) -> tuple[ProductDetail, Variant]:
        pid = self._variant_product.get(variant_id)
        if pid is None:
            raise NotFound("Variant not found.")
        product = self._products[pid]
        variant = next(v for v in product.variants if v.id == variant_id)
        return product, variant

    def _cart_view(self, customer_id: str) -> CartView:
        lines = []
        for line in self._carts.get(customer_id, {}).values():
            product, variant = self._variant(line.variant_id)
            unit = quantize(variant.price)
            lines.append(
                CartLine(
                    line_id=line.line_id,
                    product_id=product.id,
                    variant_id=variant.id,
                    name=product.name,
                    quantity=line.quantity,
                    unit_price=unit,
                    line_total=quantize(unit * line.quantity),
                )
            )
        subtotal = quantize(sum((ln.line_total for ln in lines), Decimal("0")))
        return CartView(lines=tuple(lines), subtotal=subtotal, currency=CURRENCY)

    def _own_line(self, customer_id: str, line_id: str) -> _Line:
        self._customer(customer_id)
        cart = self._carts.get(customer_id, {})
        if line_id in cart:
            return cart[line_id]
        if any(line_id in other for other in self._carts.values()):
            raise NotAuthorized("Cart line belongs to another customer.")
        raise NotFound("Cart line not found.")

    def _order_view(self, order: _Order) -> OrderStatusView:
        return OrderStatusView(
            order_number=order.order_number,
            order_status=order.order_status,
            payment_status=order.payment_status,
            events=tuple(order.events),
            eta_start=order.eta_start,
            eta_end=order.eta_end,
            tracking_id=order.tracking_id,
        )

    def _order(self, order_number: str) -> _Order:
        order = self._orders.get(order_number.strip().upper())
        if order is None:
            raise NotFound("Order not found.")
        return order

    # ------------------------------------------------------------------ CustomerPort

    def get_or_create_customer(self, whatsapp_id: str) -> CustomerRef:
        if not whatsapp_id or not str(whatsapp_id).strip():
            raise ValueError("whatsapp_id is required.")
        with self._txn():
            whatsapp_id = str(whatsapp_id).strip()
            customer_id = self._customer_by_wa.get(whatsapp_id)
            created = customer_id is None
            if created:
                self._customer_seq += 1
                customer_id = f"cust_{self._customer_seq}"
                self._customers[customer_id] = CustomerRef(
                    id=customer_id,
                    whatsapp_id=whatsapp_id,
                    name=None,
                    city=None,
                    language=None,
                    diet=None,
                    onboarding_complete=False,
                )
                self._customer_by_wa[whatsapp_id] = customer_id
                self._changed()
            self._log("get_or_create_customer", customer=customer_id, created=created)
            return self._customers[customer_id]

    def update_profile(
        self,
        customer_id: str,
        *,
        name: str | None = None,
        city: str | None = None,
        language: str | None = None,
        diet: str | None = None,
        onboarding_complete: bool | None = None,
    ) -> CustomerRef:
        if language is not None and language not in LANGUAGES:
            raise ValueError("Unsupported language.")
        if diet is not None and diet not in DIETS:
            raise ValueError("Unsupported diet.")
        with self._txn():
            customer = self._customer(customer_id)
            changes = {
                k: v
                for k, v in {
                    "name": name.strip() if isinstance(name, str) else name,
                    "city": city.strip() if isinstance(city, str) else city,
                    "language": language,
                    "diet": diet,
                    "onboarding_complete": onboarding_complete,
                }.items()
                if v is not None
            }
            customer = replace(customer, **changes)
            self._customers[customer_id] = customer
            self._changed()
            self._log("update_profile", customer=customer_id, fields=",".join(sorted(changes)))
            return customer

    # ------------------------------------------------------------------ CatalogPort

    def list_categories(self, parent_id: str | None = None) -> list[Category]:
        with self._lock:
            if parent_id is not None and parent_id not in self._categories:
                raise NotFound("Category not found.")
            result = sorted(
                (c for c in self._categories.values() if c.parent_id == parent_id),
                key=lambda c: c.sort_order,
            )
            self._log("list_categories", parent=parent_id, count=len(result))
            return result

    def list_products(
        self,
        *,
        category_id: str | None = None,
        search: str | None = None,
        diet: str | None = None,
        tag: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[ProductSummary], int]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
        if offset < 0:
            raise ValueError("offset must be >= 0.")
        if diet is not None and diet not in DIETS:
            raise ValueError("Unsupported diet.")
        with self._lock:
            products = list(self._products.values())
            if category_id is not None:
                if category_id not in self._categories:
                    raise NotFound("Category not found.")
                wanted = {category_id} | {
                    c.id for c in self._categories.values() if c.parent_id == category_id
                }
                products = [p for p in products if p.category_id in wanted]
            if search:
                needle = search.casefold().strip()
                products = [p for p in products if needle in p.name.casefold()]
            if diet is not None:
                allowed = _DIET_ALLOWS[diet]
                products = [p for p in products if p.diet_type in allowed]
            if tag is not None:
                products = [p for p in products if tag in p.tags]
            products.sort(key=self._sort_key)
            page = [self._summary(p) for p in products[offset : offset + limit]]
            self._log("list_products", total=len(products), returned=len(page))
            return page, len(products)

    def get_product(self, product_id: str) -> ProductDetail:
        with self._lock:
            product = self._products.get(product_id)
            self._log("get_product", product=product_id, found=product is not None)
            if product is None:
                raise NotFound("Product not found.")
            return product

    def resolve_product_names(self, text: str, limit: int = 5) -> list[ProductSummary]:
        """Case-insensitive substring and token match on product names."""
        with self._lock:
            query = (text or "").casefold().strip()
            query_tokens = {t for t in _tokens(query) if len(t) >= 3}
            scored = []
            if query:
                for p in self._products.values():
                    name = p.name.casefold()
                    name_tokens = set(_tokens(name))
                    if name == query:
                        score = 1000
                    elif query in name or name in query:
                        score = 500
                    else:
                        score = 0
                    score += 10 * len(query_tokens & name_tokens)
                    if score:
                        scored.append((-score, self._sort_key(p), p))
            scored.sort(key=lambda item: (item[0], item[1]))
            result = [self._summary(p) for _, _, p in scored[: max(limit, 0)]]
            self._log("resolve_product_names", matches=len(result))
            return result

    # ------------------------------------------------------------------ CartPort

    def get_cart(self, customer_id: str) -> CartView:
        with self._txn():
            self._customer(customer_id)
            view = self._cart_view(customer_id)
            self._log("get_cart", customer=customer_id, lines=len(view.lines))
            return view

    def add_item(self, customer_id: str, variant_id: str, quantity: int) -> CartView:
        _check_quantity(quantity)
        with self._txn():
            self._customer(customer_id)
            product, variant = self._variant(variant_id)
            if not (product.in_stock and variant.in_stock):
                raise ProductUnavailable("Product is out of stock.")
            cart = self._carts.setdefault(customer_id, {})
            existing = next((ln for ln in cart.values() if ln.variant_id == variant_id), None)
            if existing is not None:
                existing.quantity = _check_quantity(existing.quantity + quantity)
            else:
                self._line_seq += 1
                line_id = f"line_{self._line_seq}"
                cart[line_id] = _Line(line_id=line_id, variant_id=variant_id, quantity=quantity)
            self._changed()
            self._log("add_item", customer=customer_id, variant=variant_id, qty=quantity)
            return self._cart_view(customer_id)

    def update_quantity(self, customer_id: str, line_id: str, quantity: int) -> CartView:
        _check_quantity(quantity)
        with self._txn():
            line = self._own_line(customer_id, line_id)
            line.quantity = quantity
            self._changed()
            self._log("update_quantity", customer=customer_id, line=line_id, qty=quantity)
            return self._cart_view(customer_id)

    def remove_item(self, customer_id: str, line_id: str) -> CartView:
        with self._txn():
            self._own_line(customer_id, line_id)
            del self._carts[customer_id][line_id]
            self._changed()
            self._log("remove_item", customer=customer_id, line=line_id)
            return self._cart_view(customer_id)

    # ------------------------------------------------------------------ AddressPort

    def list_addresses(self, customer_id: str) -> list[Address]:
        with self._txn():
            self._customer(customer_id)
            result = [a for a, owner in self._addresses.values() if owner == customer_id]
            self._log("list_addresses", customer=customer_id, count=len(result))
            return result

    def add_address(self, customer_id: str, text: str, label: str | None = None) -> Address:
        if not text or not text.strip():
            raise ValueError("Address text is required.")
        with self._txn():
            self._customer(customer_id)
            self._address_seq += 1
            address = Address(
                id=f"addr_{self._address_seq}",
                label=label.strip() if label else None,
                text=text.strip(),
            )
            self._addresses[address.id] = (address, customer_id)
            self._changed()
            self._log("add_address", customer=customer_id, address=address.id)
            return address

    # ------------------------------------------------------------------ CheckoutPort

    def checkout(self, customer_id: str, address_id: str, idempotency_key: str) -> CheckoutResult:
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required.")
        with self._txn():
            self._customer(customer_id)
            previous = self._checkouts.get((customer_id, idempotency_key))
            if previous is not None:
                self._log("checkout", customer=customer_id, replay=True)
                return previous

            entry = self._addresses.get(address_id)
            if entry is None:
                raise NotFound("Address not found.")
            if entry[1] != customer_id:
                raise NotAuthorized("Address belongs to another customer.")

            view = self._cart_view(customer_id)
            if not view.lines:
                raise CartEmpty("Cart is empty.")
            for line in view.lines:
                product, variant = self._variant(line.variant_id)
                if not (product.in_stock and variant.in_stock):
                    raise CheckoutRejected(CheckoutRejected.PRODUCT_UNAVAILABLE)

            order_number = f"IVA{FIRST_ORDER_NUMBER + len(self._orders)}"
            order = _Order(
                order_number=order_number,
                customer_id=customer_id,
                address_id=address_id,
                total=view.subtotal,
                currency=view.currency,
                order_status=OrderStatus.PENDING.value,
                payment_status=PaymentStatus.PENDING.value,
                payment_url=FAKE_PAYMENT_URL.format(order_number=order_number),
                events=[OrderStatusEvent(OrderStatus.PENDING.value, self._clock())],
            )
            self._orders[order_number] = order
            self._carts[customer_id] = {}
            result = CheckoutResult(
                order_number=order_number,
                order_status=order.order_status,
                payment_status=order.payment_status,
                total=order.total,
                currency=order.currency,
                payment_url=order.payment_url,
            )
            self._checkouts[(customer_id, idempotency_key)] = result
            self._changed()
            self._log("checkout", customer=customer_id, order=order_number, replay=False)
            return result

    # ------------------------------------------------------------------ OrderPort

    def _customer_orders(self, customer_id: str) -> list[_Order]:
        # Orders dict preserves creation order; newest last.
        return [o for o in self._orders.values() if o.customer_id == customer_id]

    def get_latest_order(self, customer_id: str) -> OrderStatusView | None:
        with self._txn():
            self._customer(customer_id)
            orders = self._customer_orders(customer_id)
            self._log("get_latest_order", customer=customer_id, found=bool(orders))
            return self._order_view(orders[-1]) if orders else None

    def get_order(self, customer_id: str, order_number: str) -> OrderStatusView | None:
        with self._txn():
            self._customer(customer_id)
            order = self._orders.get((order_number or "").strip().upper())
            self._log("get_order", customer=customer_id, found=order is not None)
            if order is None:
                return None
            if order.customer_id != customer_id:
                raise NotAuthorized("Order belongs to another customer.")
            return self._order_view(order)

    def list_recent_orders(self, customer_id: str, limit: int = 5) -> list[OrderStatusView]:
        with self._txn():
            self._customer(customer_id)
            orders = self._customer_orders(customer_id)[::-1][: max(limit, 0)]
            self._log("list_recent_orders", customer=customer_id, count=len(orders))
            return [self._order_view(o) for o in orders]

    # ------------------------------------------------------------------ test-only helpers
    # Not part of the Backend Port. They stand in for the payment provider and the
    # fulfilment team so tests can drive an order through its lifecycle.

    def simulate_payment_confirmed(self, order_number: str) -> OrderStatusView:
        """Payment -> PAID, then order -> CONFIRMED, each with an event."""
        with self._txn():
            order = self._order(order_number)
            if order.payment_status not in (PaymentStatus.PENDING, PaymentStatus.FAILED):
                raise ValueError("Payment is not awaiting confirmation.")
            order.payment_status = PaymentStatus.PAID.value
            order.events.append(OrderStatusEvent(payment_event(PaymentStatus.PAID), self._clock()))
            if order.order_status == OrderStatus.PENDING:
                order.order_status = OrderStatus.CONFIRMED.value
                order.events.append(OrderStatusEvent(OrderStatus.CONFIRMED.value, self._clock()))
            self._changed()
            self._log("simulate_payment_confirmed", order=order.order_number)
            return self._order_view(order)

    def simulate_payment_failed(self, order_number: str) -> OrderStatusView:
        """Payment -> FAILED. The order stays PENDING."""
        with self._txn():
            order = self._order(order_number)
            if order.payment_status != PaymentStatus.PENDING:
                raise ValueError("Payment is not pending.")
            order.payment_status = PaymentStatus.FAILED.value
            order.events.append(
                OrderStatusEvent(payment_event(PaymentStatus.FAILED), self._clock())
            )
            self._changed()
            self._log("simulate_payment_failed", order=order.order_number)
            return self._order_view(order)

    def simulate_status(
        self,
        order_number: str,
        status: str,
        *,
        eta_start: datetime | None = None,
        eta_end: datetime | None = None,
        tracking_id: str | None = None,
    ) -> OrderStatusView:
        """Force an order status (and optionally ETA / tracking) with an event."""
        status = OrderStatus(status).value
        with self._txn():
            order = self._order(order_number)
            order.order_status = status
            order.events.append(OrderStatusEvent(status, self._clock()))
            if eta_start is not None or eta_end is not None:
                order.eta_start, order.eta_end = eta_start, eta_end
            if tracking_id is not None:
                order.tracking_id = tracking_id
            self._changed()
            self._log("simulate_status", order=order.order_number, status=status)
            return self._order_view(order)

    def set_stock(self, product_id: str, in_stock: bool) -> None:
        """Mark a product and all its variants in or out of stock."""
        with self._lock:
            product = self._products.get(product_id)
            if product is None:
                raise NotFound("Product not found.")
            variants = tuple(replace(v, in_stock=in_stock) for v in product.variants)
            self._products[product_id] = replace(product, in_stock=in_stock, variants=variants)
            self._log("set_stock", product=product_id, in_stock=in_stock)

    def set_price(self, product_id: str, price: Decimal | int | str) -> None:
        """Change a product's price and all its variants' prices (e.g. to test price_changed)."""
        amount = quantize(price)
        with self._lock:
            product = self._products.get(product_id)
            if product is None:
                raise NotFound("Product not found.")
            variants = tuple(replace(v, price=amount) for v in product.variants)
            self._products[product_id] = replace(product, price=amount, variants=variants)
            self._log("set_price", product=product_id)

    def reset(self) -> None:
        """Discard all customers, carts, addresses and orders and reload the fixture.
        With a state file configured, the saved state is cleared too."""
        with self._lock:
            self._reset_state()
            if self._state_file is not None:
                self._save_state_file()
            self._log("reset")

    def _reset_state(self) -> None:
        with self._lock:
            self._load_fixture()
            self._customers: dict[str, CustomerRef] = {}
            self._customer_by_wa: dict[str, str] = {}
            self._carts: dict[str, dict[str, _Line]] = {}
            self._addresses: dict[str, tuple[Address, str]] = {}
            self._orders: dict[str, _Order] = {}
            self._checkouts: dict[tuple[str, str], CheckoutResult] = {}
            self._customer_seq = 0
            self._line_seq = 0
            self._address_seq = 0

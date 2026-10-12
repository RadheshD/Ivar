"""Meta Commerce Manager catalog export, built only from the Backend Port."""

from __future__ import annotations

import csv
import ipaddress
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from .backend_port import MAX_PAGE_SIZE, BackendPort, NotFound, ProductSummary

SITE_ROOT = "https://ivar-eyw.vercel.app"
BRAND = "Ivar"
COLUMNS = (
    "id",
    "title",
    "description",
    "availability",
    "condition",
    "price",
    "link",
    "image_link",
    "brand",
)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".invalid")


@dataclass
class ExportResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    total: int = 0
    problems: dict[str, list[str]] = field(default_factory=dict)  # product id -> problems

    @property
    def ok(self) -> bool:
        return not self.problems


def is_public_https_url(url: str | None) -> bool:
    """True for an https URL whose host is not localhost, a private IP or a dotless name."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme != "https" or not host:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def format_price(amount: Decimal | None, currency: str | None) -> str:
    if amount is None or amount < 0:
        return ""
    return f"{amount.quantize(Decimal('0.01'))} {currency or 'INR'}"


def _all_products(backend: BackendPort) -> Iterator[ProductSummary]:
    offset = 0
    while True:
        page, total = backend.list_products(limit=MAX_PAGE_SIZE, offset=offset)
        yield from page
        offset += len(page)
        if not page or offset >= total:
            return


def collect_catalog(backend: BackendPort) -> ExportResult:
    """Read every product through the port and validate it for Meta."""
    result = ExportResult()
    seen: set[str] = set()
    for summary in _all_products(backend):
        result.total += 1
        pid = summary.id
        try:
            description = (backend.get_product(pid).description or "").strip()
        except NotFound:
            description = ""
        title = (summary.name or "").strip()
        price = format_price(summary.price, summary.currency)
        image = summary.image_url or ""
        problems: list[str] = []
        if not title:
            problems.append("missing title")
        if not price:
            problems.append("missing price")
        if not image:
            problems.append("missing image_link")
        elif not is_public_https_url(image):
            problems.append("image_link is not a public https URL")
        if pid in seen:
            problems.append("duplicate id")
        seen.add(pid)
        if problems:
            result.problems.setdefault(pid, []).extend(problems)
            continue
        result.rows.append(
            {
                "id": pid,
                "title": title,
                "description": description or title,
                "availability": "in stock" if summary.in_stock else "out of stock",
                "condition": "new",
                "price": price,
                "link": SITE_ROOT,
                "image_link": image,
                "brand": BRAND,
            }
        )
    return result


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

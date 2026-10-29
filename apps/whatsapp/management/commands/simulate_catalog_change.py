"""Development only: change a product's stock or price in the Fake Backend's dev state file.

For trying reorder by hand: make a product out of stock, back in stock, or change its price,
then reorder an old order. The change is written to the dev state file
(``IVA_FAKE_BACKEND_STATE_FILE``) and nowhere else, so a running server picks it up on its
next customer operation and it survives a restart. Customer data in the file is untouched.

Same guards as ``reset_dev_data``: ``IVA_DEV_RESET_ALLOWED`` and the Fake Backend adapter.
It also refuses if the file was written in the last few seconds (a running server may be
saving it); stop the server first. ``--dry-run`` checks everything and changes nothing.
Nothing secret is read or printed, and only the file's name is shown, never its path.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.conversations.backend_port import NotFound
from apps.conversations.fake_backend import FakeBackend
from apps.iva_common.money import to_decimal
from apps.whatsapp.dev_reset import UnsafeEnvironment, check_safe_to_run

RECENT_WRITE_SECONDS = 3


def parse_price(raw: str) -> Decimal:
    try:
        price = to_decimal(raw)
    except (ValueError, TypeError):
        raise CommandError(
            f"Bad price {raw!r}: give a positive amount such as 299 or 299.50."
        ) from None
    if price <= 0:
        raise CommandError(f"Bad price {raw!r}: the price must be above zero.")
    return price


class Command(BaseCommand):
    help = (
        "Dev only: make a product out of stock or in stock, or change its price, "
        "in the dev state file."
    )

    def add_arguments(self, parser):
        parser.add_argument("product", help="Product ID, for example quinoa.")
        change = parser.add_mutually_exclusive_group(required=True)
        change.add_argument("--out-of-stock", action="store_true", help="Mark it out of stock.")
        change.add_argument("--in-stock", action="store_true", help="Mark it in stock.")
        change.add_argument("--price", default=None, help="Set its price, for example 299.50.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check and say what would change; change nothing.",
        )

    def handle(self, *args, product, out_of_stock, in_stock, price, dry_run, **options):
        try:
            check_safe_to_run()
        except UnsafeEnvironment as exc:
            raise CommandError(str(exc)) from exc
        state_file = getattr(settings, "IVA_FAKE_BACKEND_STATE_FILE", "") or ""
        if not state_file:
            raise CommandError(
                "IVA_FAKE_BACKEND_STATE_FILE is not set: there is no dev state file."
            )
        path = Path(state_file)
        amount = parse_price(price) if price is not None else None
        if (
            not dry_run
            and path.exists()
            and time.time() - path.stat().st_mtime < RECENT_WRITE_SECONDS
        ):
            raise CommandError(
                "The state file was written a moment ago, so a server may be using it. "
                "Stop the server, then run this again."
            )
        backend = FakeBackend(state_file=path)  # reads the file; the catalog is the fixture
        try:
            current = backend.get_product(product)
        except NotFound:
            raise CommandError(f"{product!r} is not in the catalog.") from None
        if out_of_stock:
            what, change = "out of stock", {"in_stock": False}
        elif in_stock:
            what, change = "in stock", {"in_stock": True}
        else:
            what, change = f"price {amount}", {"price": amount}
        if dry_run:
            self.stdout.write(
                f"Dry run: would mark {current.id} {what} in {path.name}. Nothing changed."
            )
            return
        backend.persist_catalog_override(current.id, **change)
        self.stdout.write(f"{current.id}: now {what} in {path.name}.")

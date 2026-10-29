"""Development only: change an order's status in the Fake Backend and push the matching
``OrderStatusChangeEvent`` to the real order-events endpoint view.

For trying order-status notifications by hand. It (1) sets the order's status in the dev
state file (``IVA_FAKE_BACKEND_STATE_FILE``), so order-status replies stay consistent with the
event, then (2) posts the event, authenticated with ``IVA_ORDER_EVENTS_SECRET`` (never
printed), to ``apps.whatsapp.views.order_status_events`` in this process, so authentication,
idempotency, the 24-hour window, template lookup and sending all run as for a real push. The
message goes out through whatever WhatsApp client the settings configure: point it at a
number you own. Reuse ``--event-id`` to simulate a redelivery.

Same guards as ``reset_dev_data``: ``IVA_DEV_RESET_ALLOWED`` and the Fake Backend adapter. It
also refuses if the state file was written in the last few seconds (a running server may be
saving it); stop the server first. ``--dry-run`` checks everything and changes nothing.
No phone number, secret or path is read out or printed; only the file's name is shown.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from apps.conversations.backend_port import NotFound, OrderStatus
from apps.conversations.fake_backend import FakeBackend
from apps.whatsapp.dev_reset import UnsafeEnvironment, check_safe_to_run
from apps.whatsapp.views import EVENTS_SECRET_HEADER, order_status_events

RECENT_WRITE_SECONDS = 3
STATUSES = tuple(s.value for s in OrderStatus)


class Command(BaseCommand):
    help = "Dev only: set an order's status in the dev state file and push the event."

    def add_arguments(self, parser):
        parser.add_argument("order_number", help="Order number, for example IVA1001.")
        parser.add_argument("status", choices=STATUSES, help="The order's new status.")
        parser.add_argument(
            "--event-id", default="", help="Reuse one to simulate a redelivered event."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Check and say what would happen; do nothing."
        )

    def handle(self, *args, order_number, status, event_id, dry_run, **options):
        try:
            check_safe_to_run()
        except UnsafeEnvironment as exc:
            raise CommandError(str(exc)) from exc
        state_file = getattr(settings, "IVA_FAKE_BACKEND_STATE_FILE", "") or ""
        if not state_file:
            raise CommandError(
                "IVA_FAKE_BACKEND_STATE_FILE is not set: there is no dev state file."
            )
        secret = getattr(settings, "IVA_ORDER_EVENTS_SECRET", "")
        if not secret:
            raise CommandError("IVA_ORDER_EVENTS_SECRET is not set.")
        path = Path(state_file)
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
            customer_id = backend.order_owner(order_number)
        except NotFound:
            raise CommandError("That order is not in the dev state file.") from None
        event_id = event_id or f"sim-{secrets.token_hex(6)}"
        if dry_run:
            self.stdout.write(
                f"Dry run: would set the order to {status} in {path.name} and push event "
                f"{event_id}. Nothing changed."
            )
            return
        backend.simulate_status(order_number, status)
        body = json.dumps(
            {
                "event_id": event_id,
                "order_number": order_number.strip().upper(),
                "customer_id": customer_id,
                "new_status": status,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
        ).encode("utf-8")
        header = "HTTP_" + EVENTS_SECRET_HEADER.upper().replace("-", "_")
        request = RequestFactory().post(
            "/webhooks/whatsapp/order-events/",
            data=body,
            content_type="application/json",
            **{header: secret},
        )
        response = order_status_events(request)
        self.stdout.write(
            f"Order set to {status} in {path.name}; event {event_id} pushed, "
            f"endpoint status {response.status_code}."
        )
        if response.status_code != 200:
            raise CommandError(f"The endpoint returned {response.status_code}.")

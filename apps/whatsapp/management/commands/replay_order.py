"""Development only: replay a simulated native WhatsApp catalog order through the webhook.

Builds a valid Cloud API "order" payload from ``slug:qty`` arguments (optionally
``slug:qty:price`` to simulate the price a customer's cart showed), signs it with the dev
app secret from the environment (never printed) and posts it to the real webhook view, so
signature verification, deduplication, the inline/worker processing path and the engine
all run exactly as for a real delivery. Replies are sent through whatever WhatsApp client
the settings configure, so point it at a number you own.

Refuses to run unless ``settings.IVA_DEV_RESET_ALLOWED`` is on (only the dev settings
module sets it; the same switch ``reset_dev_data`` uses).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from apps.whatsapp.security import SIGNATURE_HEADER, SIGNATURE_PREFIX
from apps.whatsapp.views import webhook

WA_ID_ENV = "IVA_REPLAY_WA_ID"


def build_payload(
    items: list[tuple[str, str, str | None]],
    *,
    wa_id: str,
    phone_number_id: str,
    catalog_id: str,
    message_id: str,
) -> dict:
    product_items = []
    for slug, quantity, price in items:
        item = {
            "product_retailer_id": slug,
            "quantity": int(quantity) if quantity.lstrip("-").isdigit() else quantity,
            "currency": "INR",
        }
        if price is not None:
            item["item_price"] = float(price)
        product_items.append(item)
    message = {
        "from": wa_id,
        "id": message_id,
        "timestamp": str(int(time.time())),
        "type": "order",
        "order": {"catalog_id": catalog_id, "text": "", "product_items": product_items},
    }
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Replay"}, "wa_id": wa_id}],
        "messages": [message],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "REPLAY", "changes": [{"field": "messages", "value": value}]}],
    }


def parse_item(arg: str) -> tuple[str, str, str | None]:
    parts = arg.split(":")
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise CommandError(f"Bad item {arg!r}: use slug:qty or slug:qty:price.")
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


class Command(BaseCommand):
    help = "Dev only: replay a simulated native catalog order through the signed webhook."

    def add_arguments(self, parser):
        parser.add_argument("items", nargs="+", help="slug:qty or slug:qty:price")
        parser.add_argument(
            "--to",
            default="",
            help=f"Customer WhatsApp ID (default: ${WA_ID_ENV}). Never printed.",
        )
        parser.add_argument("--catalog-id", default="", help="Default: IVA_CATALOG_ID or a dummy.")
        parser.add_argument("--message-id", default="", help="Reuse one to simulate a redelivery.")

    def handle(self, *args, items, to, catalog_id, message_id, **options):
        if not getattr(settings, "IVA_DEV_RESET_ALLOWED", False):
            raise CommandError("replay_order only runs with the development settings.")
        secret = getattr(settings, "WHATSAPP_APP_SECRET", "")
        phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
        wa_id = to or os.environ.get(WA_ID_ENV, "")
        if not secret or not phone_number_id:
            raise CommandError("WHATSAPP_APP_SECRET and WHATSAPP_PHONE_NUMBER_ID must be set.")
        if not wa_id:
            raise CommandError(f"Give --to or set {WA_ID_ENV} (the customer's WhatsApp ID).")
        parsed = [parse_item(i) for i in items]
        message_id = message_id or f"wamid.REPLAY.{secrets.token_hex(8)}"
        payload = build_payload(
            parsed,
            wa_id=wa_id,
            phone_number_id=phone_number_id,
            catalog_id=catalog_id or getattr(settings, "IVA_CATALOG_ID", "") or "replay-catalog",
            message_id=message_id,
        )
        body = json.dumps(payload).encode("utf-8")
        signature = SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = "HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_")
        request = RequestFactory().post(
            "/webhooks/whatsapp/", data=body, content_type="application/json", **{header: signature}
        )
        response = webhook(request)
        self.stdout.write(
            f"Replayed order message {message_id}: {len(parsed)} item(s), "
            f"webhook status {response.status_code}."
        )
        if response.status_code != 200:
            raise CommandError(f"Webhook returned {response.status_code}.")

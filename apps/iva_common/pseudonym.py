"""Keyed pseudonyms for WhatsApp IDs (phone numbers).

A plain SHA-256 of a phone number can be reversed by trying every number, so the
pseudonym is an HMAC keyed with a dedicated secret, ``settings.IVA_PSEUDONYM_KEY``
(never ``SECRET_KEY``, so rotating Django's key does not touch conversation records).
The key must be carried over unchanged when the apps move to another project;
rotating it starts new sessions and new conversation records.
"""

import hashlib
import hmac

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_NAMESPACE = b"iva:whatsapp-id:v1:"
MIN_KEY_LENGTH = 32


def pseudonym_key() -> bytes:
    key = getattr(settings, "IVA_PSEUDONYM_KEY", "") or ""
    if len(key) < MIN_KEY_LENGTH:
        raise ImproperlyConfigured(
            f"IVA_PSEUDONYM_KEY must be set (at least {MIN_KEY_LENGTH} characters)."
        )
    return key.encode("utf-8")


def pseudonymize(value: str) -> str:
    """64-character hex pseudonym for ``value``."""
    return hmac.new(
        _NAMESPACE + pseudonym_key(), str(value).encode("utf-8"), hashlib.sha256
    ).hexdigest()

"""Conversation session state and the stores that keep it (temporary, with a TTL).

Sessions hold only navigation state (current step, list page, pending choice, checkout
idempotency key). Customer profile, cart and orders always come from the Backend Port.
Store keys are a keyed pseudonym (HMAC) of the WhatsApp ID, so no phone number is
ever a Redis key.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Protocol, runtime_checkable

from django.conf import settings

from apps.iva_common.pseudonym import pseudonymize

logger = logging.getLogger(__name__)

SESSION_VERSION = 1
DEFAULT_TTL_SECONDS = 3600
KEY_PREFIX = "iva:conv:session:"
MAX_REFS = 200


class State(StrEnum):
    NEW = "NEW"
    LANGUAGE = "LANGUAGE"
    ONBOARD_DIET = "ONBOARD_DIET"  # also used later, to change the food preference
    MAIN_MENU = "MAIN_MENU"
    GROUP = "GROUP"
    CATEGORY = "CATEGORY"
    PRODUCT = "PRODUCT"
    QUANTITY = "QUANTITY"
    CART = "CART"
    CHECKOUT_ADDRESS = "CHECKOUT_ADDRESS"
    CHECKOUT_CONFIRM = "CHECKOUT_CONFIRM"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"


@dataclass
class Session:
    state: State = State.NEW
    # Current list: {"kind": "category"|"search"|"group"|"ids", ..., "offset": int, "ids": [...]}
    listing: dict | None = None
    product_id: str | None = None
    # What the next message completes, e.g. {"kind": "add", "variant_id": ...}.
    pending: dict | None = None
    address_id: str | None = None
    idempotency_key: str | None = None
    order_number: str | None = None
    payment_url: str | None = None
    # Variant IDs resolved from a received plan's picks, awaiting "Add all to cart".
    plan_picks: list[str] = field(default_factory=list)
    # Short tokens for backend IDs that do not fit a reply ID.
    refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["version"] = SESSION_VERSION
        return data

    @classmethod
    def from_dict(cls, data: object) -> Session | None:
        """Rebuild a session; ``None`` if the data is unusable (treated as no session)."""
        if not isinstance(data, dict) or data.get("version") != SESSION_VERSION:
            return None
        try:
            values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
            values["state"] = State(values.get("state", State.NEW))
            session = cls(**values)
        except (TypeError, ValueError):
            return None
        if not isinstance(session.refs, dict) or not isinstance(session.plan_picks, list):
            return None
        return session

    def remember_ref(self, token: str, real_id: str) -> None:
        self.refs[token] = real_id
        while len(self.refs) > MAX_REFS:
            self.refs.pop(next(iter(self.refs)))


def session_key(whatsapp_id: str) -> str:
    return KEY_PREFIX + pseudonymize(whatsapp_id)


class SessionStoreError(Exception):
    """The session store could not be reached."""


@runtime_checkable
class SessionStore(Protocol):
    def load(self, whatsapp_id: str) -> Session | None: ...

    def save(self, whatsapp_id: str, session: Session) -> None: ...

    def delete(self, whatsapp_id: str) -> None: ...


class InMemorySessionStore:
    """Process-local store for tests and single-process development. Honours the TTL."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0.")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, str]] = {}

    def load(self, whatsapp_id: str) -> Session | None:
        key = session_key(whatsapp_id)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, raw = entry
            if self._clock() >= expires_at:
                del self._data[key]
                return None
        return Session.from_dict(json.loads(raw))

    def save(self, whatsapp_id: str, session: Session) -> None:
        raw = json.dumps(session.to_dict())
        with self._lock:
            self._data[session_key(whatsapp_id)] = (self._clock() + self.ttl_seconds, raw)

    def delete(self, whatsapp_id: str) -> None:
        with self._lock:
            self._data.pop(session_key(whatsapp_id), None)


class RedisSessionStore:
    """Sessions in Redis (``CONVERSATION_REDIS_URL``) with a TTL on every write."""

    def __init__(
        self, url: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS, *, client=None
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0.")
        if client is None:
            if not url:
                raise ValueError("A Redis URL or client is required.")
            import redis

            client = redis.Redis.from_url(
                url,
                socket_timeout=2,
                socket_connect_timeout=2,
                decode_responses=True,
                ssl_cert_reqs=ssl.CERT_REQUIRED,
            )
        self.ttl_seconds = ttl_seconds
        self._client = client

    def _call(self, operation: str, *args, **kwargs):
        try:
            return getattr(self._client, operation)(*args, **kwargs)
        except Exception as exc:  # redis.RedisError and socket errors alike
            raise SessionStoreError(f"Session store {operation} failed.") from exc

    def load(self, whatsapp_id: str) -> Session | None:
        raw = self._call("get", session_key(whatsapp_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return Session.from_dict(data)

    def save(self, whatsapp_id: str, session: Session) -> None:
        raw = json.dumps(session.to_dict())
        self._call("set", session_key(whatsapp_id), raw, ex=self.ttl_seconds)

    def delete(self, whatsapp_id: str) -> None:
        self._call("delete", session_key(whatsapp_id))


def ttl_from_settings() -> int:
    try:
        ttl = int(getattr(settings, "CONVERSATION_SESSION_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_TTL_SECONDS


def store_from_settings() -> SessionStore:
    """Redis when ``CONVERSATION_REDIS_URL`` is set, otherwise a process-local store."""
    url = getattr(settings, "CONVERSATION_REDIS_URL", None)
    ttl = ttl_from_settings()
    if url:
        return RedisSessionStore(url, ttl)
    logger.warning("CONVERSATION_REDIS_URL is not set: using the in-memory session store.")
    return InMemorySessionStore(ttl)

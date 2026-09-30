"""Per-customer ordering lock (S5).

Messages from the same customer must be processed one at a time, in the order they
were received. ``customer_lock`` is a Redis-backed mutex keyed by the customer's
pseudonym: while one worker (in this process or another) holds it for a WhatsApp ID,
any other message from that same customer blocks until it is released, so two
near-simultaneous messages cannot be handled concurrently or out of order. Messages
from different customers are never affected; they run fully in parallel.

The lock has two independent timings:
- ``ttl_seconds`` is a safety net only: if the holder crashes without releasing, Redis
  expires the key on its own so the customer is never stuck forever.
- ``wait_seconds`` bounds how long a second message waits for its turn before giving up.

Without a configured Redis (``CONVERSATION_REDIS_URL`` unset, e.g. bare local dev), no
ordering guarantee is possible; the lock is a no-op rather than blocking forever.
"""

from __future__ import annotations

import contextlib
import logging
import ssl
import time
import uuid
from collections.abc import Iterator

from django.conf import settings

from apps.iva_common.pseudonym import pseudonymize

logger = logging.getLogger(__name__)

KEY_PREFIX = "iva:lock:customer:"
DEFAULT_TTL_SECONDS = 30
DEFAULT_WAIT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.05
# The hard time limit SIGKILLs the worker process, which skips this context manager's
# `finally` (no clean release) — so the lock TTL must always outlive it, with margin for
# the kill and process teardown itself to happen. Derived from the setting, not a second
# hardcoded number, so the two can never silently drift apart.
LOCK_TTL_SAFETY_MARGIN_SECONDS = 10

# Release only the caller's own lock: a token mismatch means it already expired and
# someone else acquired it, so deleting it would release a lock we no longer hold.
_RELEASE_IF_OWNER = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class CustomerLockTimeout(Exception):
    """Another message from the same customer held the lock past the wait budget."""


def lock_key(whatsapp_id: str) -> str:
    return KEY_PREFIX + pseudonymize(whatsapp_id)


def default_ttl_seconds() -> float:
    """The task's own hard time limit plus a safety margin, so a slow message can never
    outlive its lock (see ``LOCK_TTL_SAFETY_MARGIN_SECONDS``). Falls back to
    ``DEFAULT_TTL_SECONDS`` if no hard time limit is configured at all."""
    hard_limit = getattr(settings, "CELERY_TASK_TIME_LIMIT", None)
    if not hard_limit:
        return DEFAULT_TTL_SECONDS
    return hard_limit + LOCK_TTL_SAFETY_MARGIN_SECONDS


def _client():
    url = getattr(settings, "CONVERSATION_REDIS_URL", None)
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url, socket_timeout=2, socket_connect_timeout=2, ssl_cert_reqs=ssl.CERT_REQUIRED
    )


@contextlib.contextmanager
def customer_lock(
    whatsapp_id: str,
    *,
    client=None,
    ttl_seconds: float | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> Iterator[None]:
    """Hold the per-customer lock for the duration of the ``with`` block.

    Raises ``CustomerLockTimeout`` if another message from the same customer is still
    being processed after ``wait_seconds``. Does nothing (no lock, no wait) when no
    Redis is configured at all. ``ttl_seconds`` defaults to ``default_ttl_seconds()``
    (resolved at call time, not import time, so ``override_settings`` in tests works).
    """
    resolved_ttl = ttl_seconds if ttl_seconds is not None else default_ttl_seconds()
    resolved_client = client if client is not None else _client()
    if resolved_client is None:
        yield
        return

    key = lock_key(whatsapp_id)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait_seconds
    acquired = False
    while True:
        try:
            acquired = bool(resolved_client.set(key, token, nx=True, px=int(resolved_ttl * 1000)))
        except Exception as exc:
            # A lock we cannot reach cannot order anything; proceed unlocked rather
            # than fail every message for every customer because Redis is briefly down.
            logger.warning(
                "customer_lock: unavailable (%s); proceeding unlocked", type(exc).__name__
            )
            yield
            return
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise CustomerLockTimeout(
                f"Timed out after {wait_seconds}s waiting for another message from this customer."
            )
        time.sleep(POLL_INTERVAL_SECONDS)

    try:
        yield
    finally:
        try:
            resolved_client.eval(_RELEASE_IF_OWNER, 1, key, token)
        except Exception as exc:
            logger.warning("customer_lock: release failed (%s)", type(exc).__name__)

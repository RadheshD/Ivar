"""Per-customer rate limit (S5).

A cap on how many inbound messages one customer's conversation is allowed to generate
replies for per minute. The webhook itself is never affected: this only decides, inside
the worker, whether ``process_inbound`` continues normally, sends one polite localized
"please slow down" reply, or stays silent. It never delays or fails the webhook's 200.

Backed by ``settings.CACHES`` (``REDIS_URL``), so the counter is shared by every worker
process. Without Redis configured, the cache falls back to per-process local memory
(see ``config/settings/base.py``), which still rate-limits a single dev process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.iva_common.pseudonym import pseudonymize

logger = logging.getLogger(__name__)

KEY_PREFIX = "iva:ratelimit:"
DEFAULT_MAX_PER_MINUTE = 20
# A little over a minute, so a message landing right at the edge of the window still
# expires with it instead of leaking into the next one.
_COUNTER_TTL_SECONDS = 90


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    # True exactly once per window: the message that first crossed the limit. The
    # caller sends the "slow down" notice only for this one, then stays silent.
    warn: bool


def limit_per_minute() -> int:
    value = getattr(settings, "IVA_RATE_LIMIT_PER_MINUTE", None)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_MINUTE
    return value if value > 0 else DEFAULT_MAX_PER_MINUTE


def _window(now) -> int:
    return int(now.timestamp() // 60)


def _key(whatsapp_id: str, window: int) -> str:
    return f"{KEY_PREFIX}{pseudonymize(whatsapp_id)}:{window}"


def register_message(whatsapp_id: str, *, now=None) -> RateLimitResult:
    """Count one inbound message toward this minute's limit for ``whatsapp_id``."""
    now = now or timezone.now()
    key = _key(whatsapp_id, _window(now))
    try:
        try:
            count = cache.incr(key)
        except ValueError:
            # First message in this window: creating an already-set-to-1 key and an
            # increment could race between workers, so treat "add lost the race" as
            # "someone else's add already put it at 1" and increment on top of it.
            if not cache.add(key, 1, timeout=_COUNTER_TTL_SECONDS):
                try:
                    count = cache.incr(key)
                except ValueError:
                    count = 1  # the key expired between add() and incr(): a fresh window
            else:
                count = 1
    except Exception as exc:
        # A limit we cannot count cannot be enforced; fail open rather than block (or
        # crash) every message for every customer because Redis is briefly down.
        logger.warning(
            "rate_limit: cache unavailable (%s); allowing the message", type(exc).__name__
        )
        return RateLimitResult(allowed=True, warn=False)
    limit = limit_per_minute()
    return RateLimitResult(allowed=count <= limit, warn=count == limit + 1)

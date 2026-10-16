"""Shared (Redis-backed) state for the Groq layer: kill switch, rate-limit pause, counters.

Everything here lives in ``django.core.cache`` (Redis when ``REDIS_URL`` is set, so every
process sees it; per-process memory otherwise). It switches off ONLY the model calls: the
deterministic code paths (price limits, diet words, sorting, goal words, safety and allergen
replies, product facts, cart guards) never read this module.

Reads that fail are treated as "model off" (fail closed): skipping an optional model call
is harmless, an uncounted one costs money. Metric writes never raise.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime

from django.core.cache import cache

from . import llm_scripts

logger = logging.getLogger(__name__)

PREFIX = "iva:llm:"
KILL_KEY = f"{PREFIX}kill"
PAUSE_KEY = f"{PREFIX}pause"
_DAY_TTL = 3 * 24 * 3600
_MIN_PAUSE_S = 1
_MAX_PAUSE_S = 3600
DEFAULT_PAUSE_S = 60

MANUAL = "manual"
AUTO = "auto"
# The flag is stored as a small integer (1 manual, 2 auto) so the Lua scripts can set and read
# it exactly as the plain cache calls do.
_KILL_CODES = {1: MANUAL, 2: AUTO}

# Counter names shown by ``llm_stats`` (the registry keeps reads to a fixed set of keys).
SKIP_REASONS = (
    "kill_switch",
    "paused",
    "circuit_open",
    "customer_limit",
    "customer_daily",
    "minute_cap",
    "daily_cap",
    "customer_budget",
    "minute_token_cap",
    "token_cap",
    "counters_unavailable",
    "input_limit",
    "redaction_empty",
)
FAILURE_REASONS = ("timeout", "rate_limited", "http_error", "connection", "bad_response", "error")
REJECT_REASONS = ("invalid_json", "invalid_schema", "hallucinated_slug")
CONFIRM_EVENTS = ("offered", "accepted", "declined")


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d")


def bump(key: str, delta: int, ttl: int) -> int:
    """Atomically add ``delta`` to a counter, creating it with ``ttl`` on first use."""
    conn = llm_scripts.connection(cache)
    if conn is not None:  # Redis: one command instead of an existence check plus the increment
        client, make_key = conn
        return int(llm_scripts.run(client, llm_scripts.BUMP, [make_key(key)], [delta, ttl]))
    try:
        return cache.incr(key, delta)
    except ValueError:
        if cache.add(key, delta, timeout=ttl):
            return delta
        return cache.incr(key, delta)


# ------------------------------------------------------------------ kill switch


def kill_mode() -> str | None:
    """``"manual"``, ``"auto"`` or ``None``. Raises when the store is unreadable."""
    value = cache.get(KILL_KEY)
    return _KILL_CODES.get(value, MANUAL) if value else None


def is_killed() -> bool:
    """True when the model must not be called. An unreadable flag counts as ON."""
    try:
        return kill_mode() is not None
    except Exception as exc:
        logger.warning("llm: kill switch unreadable error=%s; model off", type(exc).__name__)
        return True


def set_manual_kill() -> None:
    cache.set(KILL_KEY, 1, timeout=None)


def clear_kill() -> None:
    cache.delete(KILL_KEY)


def trip(cooloff_s: int) -> bool:
    """Auto-trip for ``cooloff_s`` seconds. Never overrides a manual switch. True if newly set."""
    try:
        return bool(cache.add(KILL_KEY, 2, timeout=max(1, cooloff_s)))
    except Exception as exc:
        logger.warning("llm: auto-trip not stored error=%s", type(exc).__name__)
        return False


# ------------------------------------------------------------------ provider pause (429)


def pause(seconds: float | None) -> None:
    """Stop calling for the period the provider advised (shared across processes)."""
    try:
        wait = int(seconds) if seconds is not None else DEFAULT_PAUSE_S
        wait = min(_MAX_PAUSE_S, max(_MIN_PAUSE_S, wait + 1))
        cache.set(PAUSE_KEY, 1, timeout=wait)
    except Exception as exc:
        logger.warning("llm: pause not stored error=%s", type(exc).__name__)


def is_paused() -> bool:
    try:
        return cache.get(PAUSE_KEY) is not None
    except Exception:
        return True


# ------------------------------------------------------------------ metrics


def stat(name: str, amount: int = 1, now: datetime | None = None) -> None:
    """Add to today's counter ``name``. No customer text or identifiers; never raises."""
    with contextlib.suppress(Exception):
        bump(f"{PREFIX}stat:{today(now)}:{name}", amount, _DAY_TTL)


def stat_names() -> list[str]:
    from .recommend import REQUEST_TYPES  # local: avoids an import cycle

    names = ["calls", "tokens"]
    names += [f"skipped.{r}" for r in SKIP_REASONS]
    names += [f"failed.{r}" for r in FAILURE_REASONS]
    names += [f"rejected.{r}" for r in REJECT_REASONS]
    names += [f"cart_confirm.{e}" for e in CONFIRM_EVENTS]
    names += [f"reco.{t}" for t in sorted({*REQUEST_TYPES, "safety_info", "price_unreadable"})]
    return names


def stats_today(now: datetime | None = None) -> dict[str, int]:
    day = today(now)
    names = stat_names()
    got = cache.get_many([f"{PREFIX}stat:{day}:{n}" for n in names])
    return {n: int(got.get(f"{PREFIX}stat:{day}:{n}") or 0) for n in names}


def counter(key: str) -> int:
    return int(cache.get(key) or 0)


# ------------------------------------------------------------------ rolling window


_WINDOW_TTL = 3600
_SAFE = re.compile(r"[^a-z_]")


def window_add(kind: str, now: datetime | None = None) -> None:
    kind = _SAFE.sub("", kind)
    minute = int((now or datetime.now(UTC)).timestamp() // 60)
    with contextlib.suppress(Exception):
        bump(f"{PREFIX}win:{kind}:{minute}", 1, _WINDOW_TTL)


def window_totals(minutes: int, now: datetime | None = None) -> dict[str, int]:
    minute = int((now or datetime.now(UTC)).timestamp() // 60)
    kinds = ("calls", "failures", "rejects")
    span = range(minute - minutes + 1, minute + 1)
    keys = {(k, m): f"{PREFIX}win:{k}:{m}" for k in kinds for m in span}
    got = cache.get_many(list(keys.values()))
    return {k: sum(int(got.get(keys[(k, m)]) or 0) for (kk, m) in keys if kk == k) for k in kinds}


def window_clear(minutes: int, now: datetime | None = None) -> None:
    minute = int((now or datetime.now(UTC)).timestamp() // 60)
    with contextlib.suppress(Exception):
        cache.delete_many(
            [
                f"{PREFIX}win:{k}:{m}"
                for k in ("calls", "failures", "rejects")
                for m in range(minute - minutes + 1, minute + 1)
            ]
        )

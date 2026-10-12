"""Protection around the LLM fallback: circuit breaker, per-customer limit, daily cap.

The LLM is an optional extra; none of this may ever raise into the caller. Counters live in
``django.core.cache`` (Redis when ``REDIS_URL`` is set, per-process memory otherwise, the
same backing store as the message rate limiter). The circuit breaker is per process: with
the single-worker deployment that is the whole system, and a second worker simply trips
independently.

A counter that cannot be read fails CLOSED here (the LLM is skipped), the opposite of the
message rate limiter: skipping an optional LLM call is harmless, an uncounted one costs money.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DAILY_KEY_PREFIX = "iva:llm:daily:"
CUSTOMER_KEY_PREFIX = "iva:llm:cust:"
_DAY_TTL_SECONDS = 26 * 3600
_MINUTE_TTL_SECONDS = 90


def _setting_int(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; lets one trial call through after
    ``cooldown`` seconds (half-open) and closes again on its success."""

    def __init__(
        self, threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic
    ):
        self.threshold = max(1, threshold)
        self.cooldown = max(0.0, cooldown)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at < self.cooldown or self._trial_in_flight:
                return False
            self._trial_in_flight = True  # half-open: exactly one trial call
            return True

    def release_trial(self) -> None:
        """Give back a half-open trial that was granted but never used."""
        with self._lock:
            self._trial_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._trial_in_flight = False
            if self._failures >= self.threshold:
                if self._opened_at is None:
                    logger.warning("llm: circuit breaker opened after %d failures", self._failures)
                self._opened_at = self._clock()


def _bump(key: str, ttl: int) -> int:
    """Increment a counter, creating it with ``ttl`` on first use."""
    try:
        return cache.incr(key)
    except ValueError:
        if cache.add(key, 1, timeout=ttl):
            return 1
        return cache.incr(key)


class LLMGuard:
    """``allow(customer_key)`` -> ``(ok, reason)``; call ``record(success)`` after the call."""

    def __init__(
        self,
        *,
        daily_cap: int | None = None,
        per_customer_per_minute: int | None = None,
        breaker: CircuitBreaker | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.daily_cap = (
            daily_cap if daily_cap is not None else _setting_int("IVA_LLM_DAILY_CAP", 500)
        )
        self.per_customer = (
            per_customer_per_minute
            if per_customer_per_minute is not None
            else _setting_int("IVA_LLM_CUSTOMER_PER_MINUTE", 3)
        )
        self.breaker = breaker or CircuitBreaker(
            _setting_int("IVA_LLM_BREAKER_FAILURES", 5),
            _setting_int("IVA_LLM_BREAKER_COOLDOWN_S", 120),
        )
        self._now = now or (lambda: datetime.now(UTC))

    def allow(self, customer_key: str) -> tuple[bool, str]:
        if not self.breaker.allow():
            return False, "circuit_open"
        try:
            now = self._now()
            minute = int(now.timestamp() // 60)
            customer_key_ = f"{CUSTOMER_KEY_PREFIX}{customer_key}:{minute}"
            if self.per_customer and _bump(customer_key_, _MINUTE_TTL_SECONDS) > self.per_customer:
                self.breaker.release_trial()
                return False, "customer_limit"
            day = now.strftime("%Y%m%d")
            if _bump(f"{DAILY_KEY_PREFIX}{day}", _DAY_TTL_SECONDS) > self.daily_cap:
                self.breaker.release_trial()
                return False, "daily_cap"
        except Exception as exc:
            self.breaker.release_trial()
            logger.warning(
                "llm: counters unavailable error=%s; skipping the LLM", type(exc).__name__
            )
            return False, "counters_unavailable"
        return True, "ok"

    def record(self, success: bool) -> None:
        (self.breaker.record_success if success else self.breaker.record_failure)()

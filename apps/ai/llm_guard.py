"""Protection around the LLM fallback: kill switch, breaker, request and token budgets.

The LLM is an optional extra; none of this may ever raise into the caller, and none of it
affects the deterministic code paths (it only decides whether a MODEL CALL may happen).
Counters live in ``django.core.cache`` (Redis when ``REDIS_URL`` is set, per-process memory
otherwise). The circuit breaker, the kill switch and the provider pause are shared through
the same store, so every worker process agrees.

A counter that cannot be read fails CLOSED here (the LLM is skipped), the opposite of the
message rate limiter: skipping an optional LLM call is harmless, an uncounted one costs money.

Token budget: a call RESERVES an estimate before it is made (atomic increment, rolled back if
over a cap) and is SETTLED to the provider's reported usage afterwards.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

from . import llm_scripts, llm_state

logger = logging.getLogger(__name__)

DAILY_KEY_PREFIX = "iva:llm:daily:"
CUSTOMER_KEY_PREFIX = "iva:llm:cust:"
CUSTOMER_DAY_KEY_PREFIX = "iva:llm:custday:"
MINUTE_KEY_PREFIX = "iva:llm:minute:"
BUDGET_DAY_PREFIX = "iva:llm:tokday:"
BUDGET_MINUTE_PREFIX = "iva:llm:tokmin:"
BUDGET_CUSTOMER_PREFIX = "iva:llm:tokcust:"
_DAY_TTL_SECONDS = 26 * 3600
_MINUTE_TTL_SECONDS = 90

# Built-in budgets used when a token cap is not configured (never "unlimited"). They sit
# below the provider's free-tier per-day and per-minute token limits.
DEFAULT_DAILY_TOKEN_CAP = 150_000
DEFAULT_MINUTE_TOKEN_CAP = 7_000
COMPLETION_TOKEN_ESTIMATE = 250


def _setting_int(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _setting_float(name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _token_cap(name: str, default: int) -> int:
    """A configured positive cap, else the built-in default (0/unset never means unlimited)."""
    value = _setting_int(name, 0)
    return value if value > 0 else default


def estimate_tokens(prompt_chars: int) -> int:
    """A rough pre-call estimate: ~4 characters per token plus the reply. It only gates the
    call; the provider's reported usage replaces it afterwards."""
    return max(1, prompt_chars) // 4 + 1 + COMPLETION_TOKEN_ESTIMATE


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; lets one trial call through after
    ``cooldown`` seconds (half-open) and closes again on its success. Per process: kept for
    tests and single-process use; the guard defaults to ``SharedBreaker``."""

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


class SharedBreaker:
    """The same breaker with its state in the shared cache (every process sees it).

    Open = a flag with a TTL equal to the cool-down. When it expires and the consecutive
    failure count is still at the threshold, exactly one trial call is allowed (a lock key);
    its success clears everything, its failure re-opens."""

    _FAILS = "iva:llm:brk:fails"
    _OPEN = "iva:llm:brk:open"
    _TRIAL = "iva:llm:brk:trial"

    def __init__(self, threshold: int, cooldown: float, trial_ttl: int = 30):
        self.threshold = max(1, threshold)
        self.cooldown = max(1, int(cooldown))
        self._trial_ttl = trial_ttl

    @property
    def is_open(self) -> bool:
        try:
            return llm_state.cache.get(self._OPEN) is not None
        except Exception:
            return True

    def allow(self) -> bool:
        c = llm_state.cache
        if c.get(self._OPEN) is not None:
            return False
        if int(c.get(self._FAILS) or 0) >= self.threshold:  # half-open
            return bool(c.add(self._TRIAL, 1, timeout=self._trial_ttl))
        return True

    def release_trial(self) -> None:
        with contextlib.suppress(Exception):
            llm_state.cache.delete(self._TRIAL)

    def record_success(self) -> None:
        with contextlib.suppress(Exception):
            llm_state.cache.delete_many([self._FAILS, self._OPEN, self._TRIAL])

    def record_failure(self) -> None:
        try:
            n = llm_state.bump(self._FAILS, 1, 3600)
            llm_state.cache.delete(self._TRIAL)
            if n >= self.threshold and llm_state.cache.add(self._OPEN, 1, timeout=self.cooldown):
                logger.warning("llm: circuit breaker opened after %d failures", n)
        except Exception as exc:
            logger.warning("llm: breaker state not stored error=%s", type(exc).__name__)


def _bump(key: str, ttl: int, delta: int = 1) -> int:
    """Increment a counter, creating it with ``ttl`` on first use."""
    try:
        return cache.incr(key, delta)
    except ValueError:
        if cache.add(key, delta, timeout=ttl):
            return delta
        return cache.incr(key, delta)


@dataclass
class Permit:
    """The result of ``acquire``. ``reason`` is a fixed category, never customer text."""

    ok: bool
    reason: str = "ok"
    customer_key: str = ""
    est_tokens: int = 0
    keys: tuple[str, ...] = ()  # token counters this permit reserved against
    scripted: bool = False  # reserved by the single-round-trip script (see llm_scripts)
    minute: int = 0
    day: str = ""


class LLMGuard:
    """``acquire(customer_key, est_tokens)`` -> ``Permit``; call ``finish(...)`` after the call.
    (``allow``/``record`` are the older tuple/boolean forms.)"""

    def __init__(
        self,
        *,
        daily_cap: int | None = None,
        per_customer_per_minute: int | None = None,
        breaker: CircuitBreaker | SharedBreaker | None = None,
        now: Callable[[], datetime] | None = None,
        customer_daily_cap: int | None = None,
        minute_cap: int | None = None,
        daily_token_cap: int | None = None,
        minute_token_cap: int | None = None,
        customer_share_pct: float | None = None,
    ):
        self.daily_cap = (
            daily_cap if daily_cap is not None else _setting_int("IVA_LLM_DAILY_CAP", 500)
        )
        self.per_customer = (
            per_customer_per_minute
            if per_customer_per_minute is not None
            else _setting_int("IVA_LLM_CUSTOMER_PER_MINUTE", 3)
        )
        self.customer_daily = (
            customer_daily_cap
            if customer_daily_cap is not None
            else _setting_int("IVA_LLM_CUSTOMER_DAILY_CAP", 30)
        )
        self.minute_cap = (
            minute_cap if minute_cap is not None else _setting_int("IVA_LLM_MINUTE_CAP", 20)
        )
        self.daily_tokens = (
            daily_token_cap
            if daily_token_cap is not None
            else _token_cap("IVA_LLM_DAILY_TOKEN_CAP", DEFAULT_DAILY_TOKEN_CAP)
        )
        self.minute_tokens = (
            minute_token_cap
            if minute_token_cap is not None
            else _token_cap("IVA_LLM_MINUTE_TOKEN_CAP", DEFAULT_MINUTE_TOKEN_CAP)
        )
        share = (
            customer_share_pct
            if customer_share_pct is not None
            else _setting_float("IVA_LLM_CUSTOMER_BUDGET_SHARE_PCT", 5.0)
        )
        self.customer_tokens = max(1, int(self.daily_tokens * min(100.0, share) / 100.0))
        self.breaker = breaker or SharedBreaker(
            _setting_int("IVA_LLM_BREAKER_FAILURES", 5),
            _setting_int("IVA_LLM_BREAKER_COOLDOWN_S", 120),
        )
        self._now = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ admission

    def _script_conn(self):
        """The Redis connection for the one-round-trip scripts, or ``None`` (plain per-command
        code is used: in-memory cache, a swapped cache, or a per-process breaker)."""
        if not isinstance(self.breaker, SharedBreaker) or cache is not llm_state.cache:
            return None
        return llm_scripts.connection(llm_state.cache)

    def _acquire_scripted(self, customer_key: str, est_tokens: int) -> Permit | None:
        conn = self._script_conn()
        if conn is None:
            return None
        client, mk = conn
        now = self._now()
        minute, day = int(now.timestamp() // 60), now.strftime("%Y%m%d")
        keys = [
            mk(k)
            for k in (
                llm_state.KILL_KEY, llm_state.PAUSE_KEY, SharedBreaker._OPEN,
                SharedBreaker._FAILS, SharedBreaker._TRIAL,
                f"{CUSTOMER_KEY_PREFIX}{customer_key}:{minute}",
                f"{CUSTOMER_DAY_KEY_PREFIX}{customer_key}:{day}",
                f"{MINUTE_KEY_PREFIX}{minute}", f"{DAILY_KEY_PREFIX}{day}",
                f"{BUDGET_CUSTOMER_PREFIX}{customer_key}:{day}",
                f"{BUDGET_MINUTE_PREFIX}{minute}", f"{BUDGET_DAY_PREFIX}{day}",
            )
        ]  # fmt: skip
        est = max(0, est_tokens)
        args = [
            self.breaker.threshold, self.breaker._trial_ttl, self.per_customer,
            self.customer_daily, self.minute_cap, self.daily_cap, est, self.customer_tokens,
            self.minute_tokens, self.daily_tokens, _MINUTE_TTL_SECONDS, _DAY_TTL_SECONDS,
        ]  # fmt: skip
        try:
            reason = llm_scripts.as_text(llm_scripts.run(client, llm_scripts.ACQUIRE, keys, args))
        except Exception as exc:
            logger.warning(
                "llm: counters unavailable error=%s; skipping the LLM", type(exc).__name__
            )
            return Permit(False, "counters_unavailable")
        if reason != "ok":
            return Permit(False, reason)
        return Permit(True, "ok", customer_key, est, (), True, minute, day)

    def acquire(self, customer_key: str, est_tokens: int = 0) -> Permit:
        scripted = self._acquire_scripted(customer_key, est_tokens)
        if scripted is not None:
            return scripted
        if llm_state.is_killed():
            return Permit(False, "kill_switch")
        if llm_state.is_paused():
            return Permit(False, "paused")
        reserved: list[str] = []
        try:
            if not self.breaker.allow():
                return Permit(False, "circuit_open")
            now = self._now()
            minute = int(now.timestamp() // 60)
            day = now.strftime("%Y%m%d")
            denial = self._count_requests(customer_key, minute, day)
            if denial is None and est_tokens > 0:
                denial = self._reserve_tokens(customer_key, minute, day, est_tokens, reserved)
            if denial is not None:
                self.breaker.release_trial()
                self._unreserve(reserved, est_tokens)
                return Permit(False, denial)
        except Exception as exc:
            self._unreserve(reserved, est_tokens)
            with contextlib.suppress(Exception):
                self.breaker.release_trial()
            logger.warning(
                "llm: counters unavailable error=%s; skipping the LLM", type(exc).__name__
            )
            return Permit(False, "counters_unavailable")
        return Permit(True, "ok", customer_key, est_tokens if reserved else 0, tuple(reserved))

    def _count_requests(self, customer_key: str, minute: int, day: str) -> str | None:
        # per-customer checks first, so one customer cannot spend the global counters
        if self.per_customer and (
            _bump(f"{CUSTOMER_KEY_PREFIX}{customer_key}:{minute}", _MINUTE_TTL_SECONDS)
            > self.per_customer
        ):
            return "customer_limit"
        if self.customer_daily and (
            _bump(f"{CUSTOMER_DAY_KEY_PREFIX}{customer_key}:{day}", _DAY_TTL_SECONDS)
            > self.customer_daily
        ):
            return "customer_daily"
        if self.minute_cap and (
            _bump(f"{MINUTE_KEY_PREFIX}{minute}", _MINUTE_TTL_SECONDS) > self.minute_cap
        ):
            return "minute_cap"
        if _bump(f"{DAILY_KEY_PREFIX}{day}", _DAY_TTL_SECONDS) > self.daily_cap:
            return "daily_cap"
        return None

    def _reserve_tokens(
        self, customer_key: str, minute: int, day: str, est: int, reserved: list[str]
    ) -> str | None:
        plan = (
            (f"{BUDGET_CUSTOMER_PREFIX}{customer_key}:{day}", self.customer_tokens,
             _DAY_TTL_SECONDS, "customer_budget"),
            (f"{BUDGET_MINUTE_PREFIX}{minute}", self.minute_tokens,
             _MINUTE_TTL_SECONDS, "minute_token_cap"),
            (f"{BUDGET_DAY_PREFIX}{day}", self.daily_tokens, _DAY_TTL_SECONDS, "token_cap"),
        )  # fmt: skip
        for key, cap, ttl, reason in plan:
            total = llm_state.bump(key, est, ttl)
            reserved.append(key)  # rolled back too if it is the counter that went over its cap
            if total > cap:
                return reason
        return None

    @staticmethod
    def _unreserve(keys: list[str], est: int) -> None:
        for key in keys:
            with contextlib.suppress(Exception):
                llm_state.bump(key, -est, _DAY_TTL_SECONDS)

    def allow(self, customer_key: str, est_tokens: int = 0) -> tuple[bool, str]:
        permit = self.acquire(customer_key, est_tokens)
        return permit.ok, permit.reason

    # ------------------------------------------------------------------ settlement

    def finish(
        self,
        permit: Permit,
        *,
        success: bool,
        tokens_used: int | None = None,
        failed: bool = False,
        rejected: bool = False,
    ) -> None:
        """Settle the reservation to the real usage, feed the breaker and the rolling window.

        ``success``: the breaker's view. ``failed``: transport/provider failure; ``rejected``:
        the reply came back but failed validation (or named an invented slug)."""
        actual = permit.est_tokens if tokens_used is None else max(0, tokens_used)
        if permit.scripted:
            self._finish_scripted(permit, success, actual, failed, rejected)
            return
        with contextlib.suppress(Exception):
            delta = actual - permit.est_tokens
            if delta:
                for key in permit.keys:
                    llm_state.bump(key, delta, _DAY_TTL_SECONDS)
            llm_state.stat("calls")
            if actual:
                llm_state.stat("tokens", actual)
        self.record(success)
        llm_state.window_add("calls")
        if failed:
            llm_state.window_add("failures")
        if rejected:
            llm_state.window_add("rejects")
        self._maybe_trip()

    def _finish_scripted(
        self, permit: Permit, success: bool, actual: int, failed: bool, rejected: bool
    ) -> None:
        """Settlement, breaker, metrics, rolling window and auto-trip in ONE Redis command."""
        conn = self._script_conn()
        if conn is None:
            return
        client, mk = conn
        span = max(1, _setting_int("IVA_LLM_TRIP_WINDOW_MIN", 5))
        wnow = int(datetime.now(UTC).timestamp() // 60)
        day = llm_state.today()
        pfx = llm_state.PREFIX

        def win(kind: str, minute: int) -> str:
            return mk(f"{pfx}win:{kind}:{minute}")

        minutes = range(wnow - span + 1, wnow + 1)
        keys = [
            mk(f"{BUDGET_CUSTOMER_PREFIX}{permit.customer_key}:{permit.day}"),
            mk(f"{BUDGET_MINUTE_PREFIX}{permit.minute}"),
            mk(f"{BUDGET_DAY_PREFIX}{permit.day}"),
            mk(f"{pfx}stat:{day}:calls"), mk(f"{pfx}stat:{day}:tokens"),
            mk(SharedBreaker._FAILS), mk(SharedBreaker._OPEN), mk(SharedBreaker._TRIAL),
            win("calls", wnow), win("failures", wnow), win("rejects", wnow), mk(llm_state.KILL_KEY),
            *[win("calls", m) for m in minutes],
            *[win("failures", m) for m in minutes],
            *[win("rejects", m) for m in minutes],
        ]  # fmt: skip
        args = [
            actual - permit.est_tokens, actual, int(success), int(failed), int(rejected),
            self.breaker.threshold, self.breaker.cooldown, _DAY_TTL_SECONDS,
            3 * 24 * 3600, llm_state._WINDOW_TTL, span,
            max(1, _setting_int("IVA_LLM_TRIP_MIN_CALLS", 10)),
            _setting_float("IVA_LLM_TRIP_FAILURE_RATE", 0.5),
            _setting_float("IVA_LLM_TRIP_REJECT_RATE", 0.5),
            max(1, _setting_int("IVA_LLM_TRIP_COOLOFF_S", 900)),
            int(permit.est_tokens > 0), _MINUTE_TTL_SECONDS,
        ]  # fmt: skip
        try:
            tripped, calls, fails, rejs, opened = (
                int(v) for v in llm_scripts.run(client, llm_scripts.FINISH, keys, args)
            )
        except Exception as exc:
            logger.warning("llm: usage not settled error=%s", type(exc).__name__)
            return
        if opened:
            logger.warning("llm: circuit breaker opened after %d failures", opened)
        if tripped:
            logger.warning(
                "llm: auto-trip kill switch on window_min=%d calls=%d failures=%d "
                "rejects=%d cooloff_s=%d",
                span, calls, fails, rejs, max(1, _setting_int("IVA_LLM_TRIP_COOLOFF_S", 900)),
            )  # fmt: skip

    def record(self, success: bool) -> None:
        (self.breaker.record_success if success else self.breaker.record_failure)()

    def note_rate_limited(self, retry_after: float | None) -> None:
        """The provider said wait: stop calling for that period (shared)."""
        llm_state.pause(retry_after)

    # ------------------------------------------------------------------ auto-trip

    def _maybe_trip(self) -> None:
        try:
            window = max(1, _setting_int("IVA_LLM_TRIP_WINDOW_MIN", 5))
            totals = llm_state.window_totals(window)
            calls = totals["calls"]
            if calls < max(1, _setting_int("IVA_LLM_TRIP_MIN_CALLS", 10)):
                return
            fail_rate = totals["failures"] / calls
            reject_rate = totals["rejects"] / calls
            if fail_rate <= _setting_float("IVA_LLM_TRIP_FAILURE_RATE", 0.5) and (
                reject_rate <= _setting_float("IVA_LLM_TRIP_REJECT_RATE", 0.5)
            ):
                return
            cooloff = max(1, _setting_int("IVA_LLM_TRIP_COOLOFF_S", 900))
            if llm_state.trip(cooloff):
                llm_state.window_clear(window)
                logger.warning(
                    "llm: auto-trip kill switch on window_min=%d calls=%d failures=%d "
                    "rejects=%d cooloff_s=%d",
                    window, calls, totals["failures"], totals["rejects"], cooloff,
                )  # fmt: skip
        except Exception as exc:
            logger.warning("llm: auto-trip check failed error=%s", type(exc).__name__)

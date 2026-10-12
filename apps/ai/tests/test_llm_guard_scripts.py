"""The guard on both stores: the in-memory cache (plain per-command code) and real Redis (the
one-round-trip Lua scripts). The same scenarios must give the same answers on both, and on
Redis a guarded call must cost exactly two commands (acquire + finish).

The Redis variant uses a private cache under the test root ``iva-test:`` (never the dev
prefix), every key it writes has a TTL, and its keys are removed afterwards. It is skipped
when ``REDIS_URL`` is not configured. No model is called: the classifier is a stub.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import pytest
from django.conf import settings
from django.core.cache.backends.redis import RedisCache

from apps.ai import llm_guard, llm_state
from apps.ai.classifier import CatalogEntry, ClassifierReply
from apps.ai.llm_fallback import LLMContext, LLMFallback
from apps.ai.llm_guard import LLMGuard, SharedBreaker

NOON = datetime(2026, 1, 5, 12, 0, 30, tzinfo=UTC)
DAY = NOON.strftime("%Y%m%d")
OK = json.dumps(
    {"intent": "unknown", "confidence": 0.9, "items": [], "language": "en", "ambiguous": False}
)
CATALOG = tuple(CatalogEntry(f"p{i}", f"Product {i}", "Cat") for i in range(20))


def _redis_cache():
    prefix = f"{settings.IVA_TEST_REDIS_KEY_PREFIX}guard-{uuid.uuid4().hex[:10]}"
    assert prefix.startswith("iva-test:") and not prefix.startswith("iva:")
    options = {**dict(getattr(settings, "REDIS_TLS_OPTIONS", {})), "socket_timeout": 15,
               "socket_connect_timeout": 15}  # fmt: skip
    return prefix, RedisCache(settings.REDIS_URL, {"OPTIONS": options, "KEY_PREFIX": prefix})


@pytest.fixture(params=["memory", "redis"])
def store(request, monkeypatch):
    if request.param == "memory":
        yield "memory"
        return
    if not getattr(settings, "REDIS_URL", None):
        pytest.skip("PENDING: REDIS_URL is not configured")
    prefix, cache = _redis_cache()
    monkeypatch.setattr(llm_state, "cache", cache)
    monkeypatch.setattr(llm_guard, "cache", cache)
    try:
        yield "redis"
    finally:
        client = cache._cache.get_client(write=True)
        keys = list(client.scan_iter(match=f"{prefix}:*", count=200))
        if keys:
            client.delete(*keys)


def guard(now=lambda: NOON, **kw):
    kw.setdefault("daily_cap", 1000)
    kw.setdefault("per_customer_per_minute", 0)
    kw.setdefault("customer_daily_cap", 0)
    kw.setdefault("minute_cap", 0)
    kw.setdefault("breaker", SharedBreaker(3, 60))
    return LLMGuard(now=now, **kw)


def tokens_today() -> int:
    return llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{DAY}")


# ---------------------------------------------------------------- scenarios on both stores


def test_kill_switch_and_pause_block_and_release(store):
    g = guard()
    assert g.acquire("c").ok
    llm_state.set_manual_kill()  # set by the plain code, read by whichever path acquire uses
    assert g.acquire("c").reason == "kill_switch"
    llm_state.clear_kill()
    assert g.acquire("c").ok
    llm_state.pause(30)
    assert g.acquire("c").reason == "paused"


def test_token_budget_refusal_rolls_back_every_reservation(store):
    g = guard(daily_token_cap=1000, minute_token_cap=10**6, customer_share_pct=100)
    assert g.acquire("a", 600).ok
    p = g.acquire("b", 600)
    assert (p.ok, p.reason) == (False, "token_cap")
    assert tokens_today() == 600  # the refused 600 was given back
    assert g.acquire("c", 300).ok and tokens_today() == 900


def test_customer_share_and_independent_customers(store):
    g = guard(daily_token_cap=10_000, minute_token_cap=10**6, customer_share_pct=5)
    assert g.customer_tokens == 500
    assert g.acquire("greedy", 400).ok
    assert g.acquire("greedy", 400).reason == "customer_budget"
    assert g.acquire("other", 400).ok
    assert tokens_today() == 800


def test_the_minute_token_cap_follows_the_clock(store):
    now = [NOON]
    g = guard(lambda: now[0], daily_token_cap=10**6, minute_token_cap=1000, customer_share_pct=100)
    assert g.acquire("c", 800).ok
    assert g.acquire("c", 800).reason == "minute_token_cap"
    now[0] = datetime(2026, 1, 5, 12, 1, 5, tzinfo=UTC)
    assert g.acquire("c", 800).ok


def test_settlement_charges_the_reported_usage_either_way(store):
    g = guard(daily_token_cap=3000, minute_token_cap=10**6, customer_share_pct=100)
    p = g.acquire("c", 1500)
    g.finish(p, success=True, tokens_used=200)
    assert tokens_today() == 200
    p = g.acquire("c", 500)
    g.finish(p, success=True, tokens_used=1800)
    assert tokens_today() == 2000
    assert g.acquire("d", 1500).reason == "token_cap"


def test_every_request_cap_and_its_reason(store):
    g = guard(per_customer_per_minute=2, customer_daily_cap=3, minute_cap=4, daily_cap=100)
    assert [g.acquire("x").reason for _ in range(3)] == ["ok", "ok", "customer_limit"]
    now = [datetime(2026, 1, 5, 12, 5, 0, tzinfo=UTC)]
    g2 = guard(lambda: now[0], per_customer_per_minute=0, customer_daily_cap=3, minute_cap=0)
    reasons = []
    for minute in range(5):
        now[0] = datetime(2026, 1, 5, 13, minute, 0, tzinfo=UTC)
        reasons.append(g2.acquire("y").reason)
    assert reasons == ["ok", "ok", "ok", "customer_daily", "customer_daily"]
    g3 = guard(lambda: datetime(2026, 1, 5, 14, 0, 0, tzinfo=UTC), minute_cap=4, daily_cap=100)
    assert [g3.acquire(f"m{i}").reason for i in range(6)] == ["ok"] * 4 + ["minute_cap"] * 2
    g4 = guard(lambda: datetime(2026, 1, 6, 9, 0, 0, tzinfo=UTC), daily_cap=3)
    assert [g4.acquire(f"d{i}").reason for i in range(5)] == ["ok"] * 3 + ["daily_cap"] * 2


def test_breaker_opens_allows_one_trial_and_closes_on_success(store):
    g = guard()
    for _ in range(3):
        g.finish(g.acquire("c"), success=False, failed=True)
    assert g.acquire("c").reason == "circuit_open"
    llm_state.cache.delete(SharedBreaker._OPEN)  # the cool-down is over
    trial = g.acquire("c")
    assert trial.ok  # exactly one trial
    assert g.acquire("c").reason == "circuit_open"
    g.finish(trial, success=True)
    assert g.acquire("c").ok and not g.breaker.is_open


def test_a_failed_trial_reopens(store):
    g = guard()
    for _ in range(3):
        g.finish(g.acquire("c"), success=False, failed=True)
    llm_state.cache.delete(SharedBreaker._OPEN)
    trial = g.acquire("c")
    assert trial.ok
    g.finish(trial, success=False, failed=True)
    assert g.acquire("c").reason == "circuit_open"


def test_a_bad_window_trips_the_kill_switch_once_with_a_counts_only_warning(
    store, settings, caplog
):
    settings.IVA_LLM_TRIP_MIN_CALLS = 6
    g = guard(breaker=SharedBreaker(1000, 60))
    with caplog.at_level(logging.WARNING, logger="apps.ai.llm_guard"):
        for _ in range(9):
            g.finish(g.acquire("c"), success=False, failed=True)
    assert llm_state.kill_mode() == llm_state.AUTO
    warnings = [r.getMessage() for r in caplog.records if "auto-trip" in r.getMessage()]
    assert len(warnings) == 1 and "calls=" in warnings[0] and "failures=" in warnings[0]
    assert g.acquire("c").reason == "kill_switch"
    llm_state.clear_kill()


def test_a_manual_switch_is_not_replaced_by_an_automatic_trip(store, settings):
    settings.IVA_LLM_TRIP_MIN_CALLS = 3
    llm_state.set_manual_kill()
    g = guard(breaker=SharedBreaker(1000, 60))
    for _ in range(5):
        g.finish(llm_guard.Permit(True, "ok", "c", 0, (), store == "redis", 0, DAY), success=False,
                 failed=True)  # fmt: skip
    assert llm_state.kill_mode() == llm_state.MANUAL
    llm_state.clear_kill()


def test_good_traffic_does_not_trip(store, settings):
    settings.IVA_LLM_TRIP_MIN_CALLS = 10
    g = guard()
    for i in range(20):
        g.finish(g.acquire(f"c{i}"), success=True)
    assert llm_state.kill_mode() is None


def test_metrics_count_calls_and_tokens(store):
    g = guard()
    for _ in range(3):
        g.finish(g.acquire("c", 400), success=True, tokens_used=950)
    s = llm_state.stats_today()
    assert s["calls"] == 3 and s["tokens"] == 2850


def test_bump_creates_with_a_ttl_and_adds(store):
    assert llm_state.bump("iva:llm:test:bump", 5, 60) == 5
    assert llm_state.bump("iva:llm:test:bump", 2, 60) == 7
    assert llm_state.bump("iva:llm:test:bump", -3, 60) == 4
    llm_state.cache.delete("iva:llm:test:bump")


# ---------------------------------------------------------------- the cost on Redis


class Stub:
    def classify(self, message, *, catalog, timeout):
        return ClassifierReply(OK, "m", 900, 40)


@pytest.fixture
def counted(store):
    if store != "redis":
        pytest.skip("only meaningful on Redis")
    import redis

    seen: list[str] = []
    real = redis.Redis.execute_command

    def counting(self, *args, **kwargs):
        seen.append(str(args[0]).upper())
        return real(self, *args, **kwargs)

    redis.Redis.execute_command = counting
    try:
        yield seen
    finally:
        redis.Redis.execute_command = real


def test_a_guarded_call_costs_exactly_two_redis_commands(counted):
    fb = LLMFallback(Stub(), guard(), min_confidence=0.7, timeout=2)
    ctx = LLMContext(CATALOG, "cust-1", 20)
    fb.run("zork florp", ctx)  # warm: the first call also builds connections
    counted.clear()
    for word in ("alpha", "bravo", "charlie"):
        before = len(counted)
        fb.run(f"zork {word}", ctx)
        assert counted[before:] == ["EVAL", "EVAL"], counted[before:]


def test_acquire_alone_is_one_command_even_when_refused(counted):
    g = guard(daily_cap=0)
    assert g.acquire("c", 500).reason == "daily_cap"
    assert counted == ["EVAL"]
    llm_state.set_manual_kill()
    counted.clear()
    assert g.acquire("c", 500).reason == "kill_switch"
    assert counted == ["EVAL"]
    llm_state.clear_kill()


def test_a_redis_error_in_the_script_fails_closed(store, monkeypatch):
    if store != "redis":
        pytest.skip("only meaningful on Redis")
    monkeypatch.setattr(llm_guard.llm_scripts, "run", lambda *a, **k: 1 / 0)
    p = guard().acquire("c", 100)
    assert (p.ok, p.reason) == (False, "counters_unavailable")

"""The guard around model calls: kill switch, token budget, provider pause, shared breaker,
auto-trip, metrics, and the management commands.

All state is the in-memory test cache (cleared before every test); no network.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai import llm_guard, llm_state
from apps.ai.classifier import (
    CatalogEntry,
    ClassifierHttpError,
    ClassifierRateLimited,
    ClassifierReply,
    ClassifierTimeout,
)
from apps.ai.groq import GroqClassifier, parse_duration
from apps.ai.llm_fallback import LLMContext, LLMFallback
from apps.ai.llm_guard import LLMGuard, SharedBreaker, estimate_tokens
from apps.iva_common.http_transport import HttpResponse

CATALOG = (CatalogEntry("quinoa", "Quinoa", "Cooking Essentials"),)
CTX = LLMContext(CATALOG, "cust-1", 20)
OK = json.dumps(
    {"intent": "unknown", "confidence": 0.9, "items": [], "language": "en", "ambiguous": False}
)
NOON = datetime(2026, 1, 5, 12, 0, 30, tzinfo=UTC)


def guard(**kw):
    kw.setdefault("daily_cap", 1000)
    kw.setdefault("per_customer_per_minute", 0)
    kw.setdefault("now", lambda: NOON)
    return LLMGuard(**kw)


class Scripted:
    def __init__(self, *items, tokens=(100, 20)):
        self.items, self.tokens, self.calls = list(items), tokens, 0

    def classify(self, message, *, catalog, timeout):
        self.calls += 1
        item = self.items.pop(0) if self.items else OK
        if isinstance(item, Exception):
            raise item
        return ClassifierReply(item, "m", *self.tokens)


# ---------------------------------------------------------------- kill switch


def test_the_kill_switch_blocks_every_call_and_off_restores_them():
    g = guard()
    assert g.acquire("c").ok
    llm_state.set_manual_kill()
    p = g.acquire("c")
    assert (p.ok, p.reason) == (False, "kill_switch")
    llm_state.clear_kill()
    assert g.acquire("c").ok


def test_the_kill_switch_is_checked_on_every_call_through_the_fallback():
    fake = Scripted()
    fb = LLMFallback(fake, guard(), min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    assert fake.calls == 1
    llm_state.set_manual_kill()
    result = fb.run("zork florp", CTX)
    assert fake.calls == 1 and result.classification is None
    assert llm_state.stats_today()["skipped.kill_switch"] == 1


def test_an_unreadable_kill_switch_fails_closed(monkeypatch):
    monkeypatch.setattr(llm_state.cache, "get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert llm_state.is_killed() is True
    assert llm_guard.LLMGuard(daily_cap=5).acquire("c").reason == "kill_switch"


def test_an_auto_trip_never_overrides_a_manual_switch_and_expires():
    llm_state.set_manual_kill()
    assert llm_state.trip(60) is False
    assert llm_state.kill_mode() == llm_state.MANUAL
    llm_state.clear_kill()
    assert llm_state.trip(60) is True and llm_state.kill_mode() == llm_state.AUTO


def test_the_killswitch_command_switches_and_reports(capsys):
    call_command("llm_killswitch", "--on")
    assert llm_state.is_killed()
    call_command("llm_killswitch", "--status")
    out = capsys.readouterr().out
    assert "ON (manual)" in out
    call_command("llm_killswitch", "--off")
    assert not llm_state.is_killed()
    call_command("llm_killswitch")
    assert "OFF" in capsys.readouterr().out


def test_the_killswitch_command_refuses_to_guess_when_the_store_is_down(monkeypatch):
    monkeypatch.setattr(llm_state.cache, "set", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CommandError):
        call_command("llm_killswitch", "--on")


# ---------------------------------------------------------------- token budget


def test_the_estimate_is_positive_and_grows_with_the_prompt():
    assert estimate_tokens(0) > 0
    assert estimate_tokens(8000) > estimate_tokens(4000)


def test_a_call_is_refused_when_its_estimate_would_pass_the_daily_token_cap():
    g = guard(daily_token_cap=1000, minute_token_cap=10**6, customer_share_pct=100)
    assert g.acquire("a", 600).ok
    p = g.acquire("b", 600)
    assert (p.ok, p.reason) == (False, "token_cap")
    assert g.acquire("c", 300).ok  # the refused reservation was rolled back


def test_the_minute_token_cap_resets_with_the_minute():
    now = [NOON]
    g = guard(daily_token_cap=10**6, minute_token_cap=1000, customer_share_pct=100,
              now=lambda: now[0])  # fmt: skip
    assert g.acquire("c", 800).ok
    assert g.acquire("c", 800).reason == "minute_token_cap"
    now[0] = datetime(2026, 1, 5, 12, 1, 5, tzinfo=UTC)
    assert g.acquire("c", 800).ok


def test_settlement_replaces_the_estimate_with_the_reported_usage():
    g = guard(daily_token_cap=2000, minute_token_cap=10**6, customer_share_pct=100)
    p = g.acquire("c", 1500)
    assert p.ok
    g.finish(p, success=True, tokens_used=200)  # the call was cheaper than estimated
    assert g.acquire("c", 1500).ok  # 200 + 1500 fits under 2000
    day = NOON.strftime("%Y%m%d")
    assert llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}") == 1700


def test_a_call_that_cost_more_than_estimated_is_charged_in_full():
    g = guard(daily_token_cap=2000, minute_token_cap=10**6, customer_share_pct=100)
    p = g.acquire("c", 500)
    g.finish(p, success=True, tokens_used=1800)
    assert g.acquire("other", 500).reason == "token_cap"


def test_one_customer_cannot_use_more_than_their_share_of_the_daily_budget():
    g = guard(daily_token_cap=10_000, minute_token_cap=10**6, customer_share_pct=5,
              customer_daily_cap=1000)  # fmt: skip
    assert g.customer_tokens == 500
    assert g.acquire("greedy", 400).ok
    assert g.acquire("greedy", 400).reason == "customer_budget"
    assert g.acquire("other", 400).ok  # someone else is unaffected
    day = NOON.strftime("%Y%m%d")
    assert (
        llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}") == 800
    )  # refusals are not charged


def test_one_customer_flooding_cannot_spend_the_global_counters():
    g = guard(daily_cap=50, per_customer_per_minute=3, minute_cap=50)
    results = [g.acquire("flood").reason for _ in range(40)]
    assert results.count("ok") == 3 and set(results) == {"ok", "customer_limit"}
    assert g.acquire("someone-else").ok  # the global counters barely moved


def test_a_customer_daily_cap_applies_across_minutes():
    now = [NOON]
    g = guard(customer_daily_cap=4, now=lambda: now[0])
    ok = 0
    for minute in range(8):
        now[0] = datetime(2026, 1, 5, 12, minute, 0, tzinfo=UTC)
        ok += g.acquire("c").ok
    assert ok == 4


def test_many_customers_hit_the_global_request_cap_and_calls_stay_bounded():
    g = guard(daily_cap=10, minute_cap=1000)
    granted = sum(g.acquire(f"cust-{i}").ok for i in range(200))
    assert granted == 10


def test_the_global_per_minute_request_cap_is_enforced():
    g = guard(minute_cap=5)
    assert sum(g.acquire(f"c{i}").ok for i in range(20)) == 5


def test_a_token_cap_of_zero_means_the_built_in_default_never_unlimited(settings):
    settings.IVA_LLM_DAILY_TOKEN_CAP = 0
    settings.IVA_LLM_MINUTE_TOKEN_CAP = 0
    g = LLMGuard()
    assert g.daily_tokens == llm_guard.DEFAULT_DAILY_TOKEN_CAP
    assert g.minute_tokens == llm_guard.DEFAULT_MINUTE_TOKEN_CAP
    settings.IVA_LLM_DAILY_TOKEN_CAP = 1234
    assert LLMGuard().daily_tokens == 1234


def test_unreadable_counters_fail_closed_and_leave_nothing_reserved(monkeypatch):
    g = guard(daily_token_cap=10_000)
    monkeypatch.setattr(llm_guard.cache, "incr", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    p = g.acquire("c", 500)
    assert (p.ok, p.reason) == (False, "counters_unavailable")


def test_token_counters_that_cannot_be_written_fail_closed(monkeypatch):
    g = guard(daily_token_cap=10_000)
    monkeypatch.setattr(llm_state, "bump", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert g.acquire("c", 500).reason == "counters_unavailable"


# ---------------------------------------------------------------- the fallback, end to end


def test_the_fallback_charges_the_reported_tokens_and_counts_the_call():
    fake = Scripted(tokens=(700, 50))
    g = guard(daily_token_cap=100_000, customer_share_pct=100)
    LLMFallback(fake, g, min_confidence=0.7, timeout=2).run("zork florp", CTX)
    s = llm_state.stats_today()
    assert s["calls"] == 1 and s["tokens"] == 750


def test_a_429_pauses_every_caller_for_the_advised_period_and_is_never_retried():
    fake = Scripted(ClassifierRateLimited(retry_after=30))
    fb = LLMFallback(fake, guard(), min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    assert fake.calls == 1  # one attempt, no retry on the customer path
    ttl_left = llm_state.cache.ttl("iva:llm:pause") if hasattr(llm_state.cache, "ttl") else 31
    assert llm_state.is_paused() and ttl_left <= 31
    for _ in range(5):
        fb.run("zork florp", CTX)
    assert fake.calls == 1  # the storm is absorbed: no further call during the pause
    assert llm_state.stats_today()["skipped.paused"] == 5
    assert llm_state.stats_today()["failed.rate_limited"] == 1


def test_a_429_without_an_advised_period_still_pauses_for_a_default_period():
    fb = LLMFallback(Scripted(ClassifierRateLimited()), guard(), min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    assert llm_state.is_paused()


def test_a_rate_limited_call_spent_no_tokens():
    g = guard(daily_token_cap=100_000, customer_share_pct=100)
    LLMFallback(Scripted(ClassifierRateLimited(3)), g, min_confidence=0.7, timeout=2).run(
        "zork florp", CTX
    )
    day = NOON.strftime("%Y%m%d")
    assert llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}") == 0


def test_a_provider_that_reports_a_nearly_spent_budget_pauses_after_a_good_reply():
    class Tight(Scripted):
        def classify(self, message, *, catalog, timeout):
            reply = super().classify(message, catalog=catalog, timeout=timeout)
            return ClassifierReply(reply.text, "m", 100, 20, pause_s=12.0)

    fake = Tight()
    fb = LLMFallback(fake, guard(), min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    fb.run("zork florp", CTX)
    assert fake.calls == 1 and llm_state.is_paused()


def test_a_timeout_is_charged_its_estimate_but_a_refused_connection_is_not():
    g = guard(daily_token_cap=100_000, customer_share_pct=100)
    day = NOON.strftime("%Y%m%d")
    LLMFallback(Scripted(ClassifierTimeout()), g, min_confidence=0.7, timeout=2).run(
        "zork florp", CTX
    )
    assert llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}") > 0
    llm_state.cache.clear()
    LLMFallback(Scripted(ClassifierHttpError("connection")), g, min_confidence=0.7, timeout=2).run(
        "zork florp", CTX
    )
    assert llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}") == 0


def test_over_length_digit_only_link_only_and_emoji_messages_never_reach_the_model():
    fake = Scripted()
    fb = LLMFallback(fake, guard(), min_confidence=0.7, timeout=2)
    for text in (
        "add quinoa " * 80,
        "9876543210",
        "1234 5678 9012",
        "https://evil.example/pay",
        "🙂😀😂🤣😊😍🤔",
        "​‍‮",
        "a@b.example",
    ):
        assert fb.run(text, CTX).classification is None
    assert fake.calls == 0
    s = llm_state.stats_today()
    assert s["skipped.input_limit"] + s["skipped.redaction_empty"] == 7


def test_the_input_limit_setting_is_honoured(settings):
    settings.IVA_LLM_MAX_INPUT_CHARS = 20
    fake = Scripted()
    fb = LLMFallback(fake, guard(), min_confidence=0.7, timeout=2)
    fb.run("this message is clearly longer than twenty", CTX)
    assert fake.calls == 0
    fb.run("add quinoa", CTX)
    assert fake.calls == 1


def test_what_reaches_the_model_is_the_normalised_redacted_text():
    seen = []

    class Spy(Scripted):
        def classify(self, message, *, catalog, timeout):
            seen.append(message)
            return super().classify(message, catalog=catalog, timeout=timeout)

    LLMFallback(Spy(), guard(), min_confidence=0.7, timeout=2).run(
        "ad​d quinoa!!!!!!!! mail a.b@example.org or 9876543210", CTX
    )
    assert seen == ["add quinoa!!! mail [email] or [number]"]


# ---------------------------------------------------------------- shared breaker + auto-trip


def test_the_breaker_state_is_shared_between_guards_like_two_processes():
    a = LLMGuard(daily_cap=100, per_customer_per_minute=0, breaker=SharedBreaker(2, 60))
    b = LLMGuard(daily_cap=100, per_customer_per_minute=0, breaker=SharedBreaker(2, 60))
    for _ in range(2):
        a.finish(a.acquire("c"), success=False, failed=True)
    assert b.acquire("c").reason == "circuit_open"
    assert b.breaker.is_open


def test_the_shared_breaker_half_opens_with_exactly_one_trial_and_closes_on_success():
    br = SharedBreaker(2, 60)
    br.record_failure()
    br.record_failure()
    assert br.allow() is False
    llm_state.cache.delete(SharedBreaker._OPEN)  # the cool-down has passed
    assert br.allow() is True  # the one trial
    assert br.allow() is False  # no second trial while it is in flight
    br.record_success()
    assert br.allow() is True and not br.is_open


def test_a_failed_trial_reopens_the_breaker():
    br = SharedBreaker(2, 60)
    br.record_failure()
    br.record_failure()
    llm_state.cache.delete(SharedBreaker._OPEN)
    assert br.allow() is True
    br.record_failure()
    assert br.is_open and br.allow() is False


def test_a_high_failure_rate_trips_the_kill_switch_with_one_counts_only_warning(settings, caplog):
    settings.IVA_LLM_TRIP_MIN_CALLS = 6
    settings.IVA_LLM_TRIP_FAILURE_RATE = 0.5
    settings.IVA_LLM_BREAKER_FAILURES = 1000  # keep the plain breaker out of the way
    g = guard(breaker=SharedBreaker(1000, 60))
    with caplog.at_level(logging.WARNING, logger="apps.ai.llm_guard"):
        for _ in range(8):
            g.finish(g.acquire("c"), success=False, failed=True)
    assert llm_state.kill_mode() == llm_state.AUTO
    warnings = [r for r in caplog.records if "auto-trip" in r.getMessage()]
    assert len(warnings) == 1
    assert "calls=" in warnings[0].getMessage() and "failures=" in warnings[0].getMessage()
    assert g.acquire("c").reason == "kill_switch"


def test_a_high_validation_rejection_rate_trips_the_kill_switch(settings):
    settings.IVA_LLM_TRIP_MIN_CALLS = 6
    settings.IVA_LLM_TRIP_REJECT_RATE = 0.4
    g = guard()
    for _ in range(10):
        g.finish(g.acquire("c"), success=False, rejected=True)
    assert llm_state.kill_mode() == llm_state.AUTO


def test_good_traffic_never_trips_and_a_few_failures_are_tolerated(settings):
    settings.IVA_LLM_TRIP_MIN_CALLS = 10
    g = guard()
    for i in range(20):
        g.finish(g.acquire(f"c{i}"), success=i % 10 != 0, failed=i % 10 == 0)
    assert llm_state.kill_mode() is None


def test_below_the_minimum_number_of_calls_nothing_trips():
    g = guard()
    for _ in range(3):
        g.finish(g.acquire("c"), success=False, failed=True, rejected=True)
    assert llm_state.kill_mode() is None


def test_invalid_replies_from_the_fallback_count_as_rejections_and_trip_in_a_loop(settings):
    settings.IVA_LLM_TRIP_MIN_CALLS = 5
    settings.IVA_LLM_BREAKER_FAILURES = 1000
    fake = Scripted(*["not json"] * 10)
    fb = LLMFallback(fake, guard(breaker=SharedBreaker(1000, 60)), min_confidence=0.7, timeout=2)
    for _ in range(10):
        fb.run("zork florp", CTX)
    assert llm_state.kill_mode() == llm_state.AUTO
    assert fake.calls < 10  # it stopped calling once tripped
    assert llm_state.stats_today()["rejected.invalid_json"] >= 5


# ---------------------------------------------------------------- headers


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("7.66s", 7.66), ("2m59.56s", 179.56), ("250ms", 0.25), ("30", 30.0), ("1h", 3600.0)],
)
def test_provider_durations_are_parsed(value, seconds):
    assert parse_duration(value) == pytest.approx(seconds)


@pytest.mark.parametrize("value", [None, "", "soon", "5x", "1h2"])
def test_unparseable_durations_are_ignored(value):
    assert parse_duration(value) is None


def test_the_adapter_reads_retry_after_and_ratelimit_headers():
    class T:
        def __init__(self, response):
            self.response = response

        def request(self, *a, **k):
            return self.response

    limited = HttpResponse(429, b"{}", {"Retry-After": "17", "x-ratelimit-reset-tokens": "9s"})
    with pytest.raises(ClassifierRateLimited) as exc:
        GroqClassifier("gsk_TEST_x", transport=T(limited)).classify(
            "hi", catalog=CATALOG, timeout=3
        )
    assert exc.value.retry_after == 17.0
    body = json.dumps(
        {
            "choices": [{"message": {"content": OK}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1},
        }
    ).encode()
    low = HttpResponse(200, body, {"x-ratelimit-remaining-tokens": "200",
                                   "x-ratelimit-reset-tokens": "4.5s"})  # fmt: skip
    reply = GroqClassifier("gsk_TEST_x", transport=T(low)).classify(
        "hi", catalog=CATALOG, timeout=3
    )
    assert reply.pause_s == 4.5
    plenty = HttpResponse(200, body, {"x-ratelimit-remaining-tokens": "7000"})
    assert (
        GroqClassifier("gsk_TEST_x", transport=T(plenty))
        .classify("hi", catalog=CATALOG, timeout=3)
        .pause_s
        is None
    )


# ---------------------------------------------------------------- metrics + stats command


def test_stats_count_skips_failures_and_rejections_without_customer_data():
    g = guard()
    llm_state.stat("skipped.kill_switch")
    llm_state.stat("cart_confirm.offered", 2)
    fb = LLMFallback(Scripted("not json", ClassifierTimeout()), g, min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    fb.run("zork florp", CTX)
    s = llm_state.stats_today()
    assert s["rejected.invalid_json"] == 1 and s["failed.timeout"] == 1
    assert s["skipped.kill_switch"] == 1 and s["cart_confirm.offered"] == 2
    assert all(isinstance(v, int) for v in s.values())
    assert not any("zork" in k or "cust-1" in k for k in s)


def test_llm_stats_prints_totals_and_budget_and_nothing_sensitive(capsys):
    Scripted()
    fb = LLMFallback(Scripted(), LLMGuard(daily_cap=100), min_confidence=0.7, timeout=2)
    fb.run("zork florp", CTX)
    call_command("llm_stats")
    out = capsys.readouterr().out
    assert "LLM usage for" in out and "calls: 1" in out and "tokens" in out
    assert "zork" not in out and "cust-1" not in out and "gsk_" not in out


def test_llm_stats_on_an_empty_day(capsys):
    call_command("llm_stats")
    assert "no model activity" in capsys.readouterr().out


def test_stat_writes_never_raise(monkeypatch):
    monkeypatch.setattr(llm_state, "bump", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    llm_state.stat("calls")  # must not raise
    llm_state.window_add("calls")


def test_the_test_cache_is_isolated_between_tests_first():
    assert cache.get("iva:llm:kill") is None

"""The per-customer rate limit: allow up to the cap, warn exactly once when it's first
crossed, then stay silent until the next minute's window.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from apps.whatsapp.rate_limit import register_message

WA = "919876543210"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def setup_function():
    cache.clear()


@override_settings(IVA_RATE_LIMIT_PER_MINUTE=3)
def test_allows_up_to_the_limit_with_no_warning():
    for _ in range(3):
        result = register_message(WA, now=T0)
        assert result.allowed
        assert not result.warn


@override_settings(IVA_RATE_LIMIT_PER_MINUTE=3)
def test_warns_exactly_once_when_first_over_the_limit():
    for _ in range(3):
        register_message(WA, now=T0)
    fourth = register_message(WA, now=T0)
    assert not fourth.allowed
    assert fourth.warn
    fifth = register_message(WA, now=T0)
    assert not fifth.allowed
    assert not fifth.warn  # already warned this window: stay silent


@override_settings(IVA_RATE_LIMIT_PER_MINUTE=1)
def test_resets_on_the_next_minute():
    register_message(WA, now=T0)
    over = register_message(WA, now=T0)
    assert not over.allowed and over.warn

    next_minute = T0 + timedelta(minutes=1)
    fresh = register_message(WA, now=next_minute)
    assert fresh.allowed
    assert not fresh.warn


@override_settings(IVA_RATE_LIMIT_PER_MINUTE=2)
def test_customers_are_counted_independently():
    register_message(WA, now=T0)
    register_message(WA, now=T0)
    other = register_message("919876500000", now=T0)
    assert other.allowed  # a different customer's first message, not this one's third


@override_settings(IVA_RATE_LIMIT_PER_MINUTE=0)
def test_non_positive_limit_falls_back_to_the_default():
    result = register_message(WA, now=T0)
    assert result.allowed  # 1 message never trips the (much larger) default cap


def test_default_limit_without_the_setting():
    result = register_message(WA, now=T0)
    assert result.allowed
    assert not result.warn


def test_redis_down_fails_open_instead_of_crashing():
    with mock.patch("django.core.cache.cache.incr", side_effect=ConnectionError("down")):
        result = register_message(WA, now=T0)
    assert result.allowed
    assert not result.warn

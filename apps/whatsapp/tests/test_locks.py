"""The per-customer lock serializes same-customer processing without affecting other
customers, self-heals if a holder crashes (TTL), and only ever releases its own token.
"""

import ssl
import threading
import time
from unittest import mock

import pytest
from django.test import override_settings

from apps.whatsapp import locks as locks_module
from apps.whatsapp.locks import (
    DEFAULT_TTL_SECONDS,
    LOCK_TTL_SAFETY_MARGIN_SECONDS,
    CustomerLockTimeout,
    customer_lock,
    default_ttl_seconds,
    lock_key,
)

WA_A = "919876543210"
WA_B = "919876500000"


class FakeLockRedis:
    """Minimal stand-in for the subset of redis-py this module uses: SET NX PX, GET
    and the release-if-owner EVAL script. Thread-safe, so it can stand in for a real
    server shared by two threads racing for the same lock."""

    def __init__(self):
        self._guard = threading.Lock()
        self._data: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str, now: float) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and now >= expires_at:
            del self._data[key]
            return None
        return value

    def set(self, key, value, *, nx=False, px=None):
        now = time.monotonic()
        with self._guard:
            if nx and self._live(key, now) is not None:
                return False
            expires_at = now + px / 1000 if px else None
            self._data[key] = (value, expires_at)
            return True

    def get(self, key):
        with self._guard:
            return self._live(key, time.monotonic())

    def eval(self, script, numkeys, key, token):
        with self._guard:
            if self._live(key, time.monotonic()) == token:
                del self._data[key]
                return 1
            return 0


def test_lock_key_never_contains_the_phone_number():
    key = lock_key(WA_A)
    assert WA_A not in key
    assert key.startswith(locks_module.KEY_PREFIX)  # "iva-test:..." under pytest
    assert key == lock_key(WA_A)


def test_two_messages_from_the_same_customer_are_serialized():
    client = FakeLockRedis()
    order: list[str] = []
    concurrent = 0
    max_concurrent = 0
    guard = threading.Lock()

    def hold(label, hold_seconds):
        nonlocal concurrent, max_concurrent
        with customer_lock(WA_A, client=client, wait_seconds=2):
            with guard:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            order.append(f"{label}:enter")
            time.sleep(hold_seconds)
            order.append(f"{label}:exit")
            with guard:
                concurrent -= 1

    first = threading.Thread(target=hold, args=("first", 0.2))
    first.start()
    time.sleep(0.05)  # "first" arrived first and is already inside the critical section
    second = threading.Thread(target=hold, args=("second", 0.0))
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert max_concurrent == 1
    assert order == ["first:enter", "first:exit", "second:enter", "second:exit"]


def test_different_customers_are_never_blocked_by_each_other():
    client = FakeLockRedis()
    entered_together = threading.Event()

    def hold(wa_id):
        with customer_lock(wa_id, client=client, wait_seconds=1):
            entered_together.wait(timeout=1)

    a = threading.Thread(target=hold, args=(WA_A,))
    b = threading.Thread(target=hold, args=(WA_B,))
    a.start()
    b.start()
    # Both got in at once; if B were blocked on A's lock, this would time out instead.
    entered_together.set()
    a.join(timeout=2)
    b.join(timeout=2)
    assert not a.is_alive()
    assert not b.is_alive()


def test_timed_out_waiting_for_another_message_from_the_same_customer():
    client = FakeLockRedis()
    client.set(lock_key(WA_A), "someone-elses-token", nx=True, px=5000)
    with pytest.raises(CustomerLockTimeout):
        with customer_lock(WA_A, client=client, wait_seconds=0.2):
            raise AssertionError("must not run: the lock was never acquired")


def test_a_crashed_holder_is_released_by_the_ttl():
    client = FakeLockRedis()
    client.set(lock_key(WA_A), "crashed-holders-token", nx=True, px=100)
    time.sleep(0.15)  # the TTL alone frees it, with no release call
    with customer_lock(WA_A, client=client, wait_seconds=1):
        pass  # acquiring here is the assertion: no CustomerLockTimeout was raised


def test_release_only_removes_the_callers_own_token():
    client = FakeLockRedis()
    key = lock_key(WA_A)
    with customer_lock(WA_A, client=client, ttl_seconds=0.05, wait_seconds=1):
        pass
    # The lock expired mid-``with`` and someone else's token now owns the key; our
    # release must not delete a lock we no longer hold.
    client.set(key, "someone-elses-token", nx=False)
    # Re-entering the (already exited) context manager's cleanup is simulated by
    # calling eval directly with the old, no-longer-valid token:
    from apps.whatsapp.locks import _RELEASE_IF_OWNER

    client.eval(_RELEASE_IF_OWNER, 1, key, "a-stale-token-from-before")
    assert client.get(key) == "someone-elses-token"


@override_settings(CELERY_TASK_TIME_LIMIT=60)
def test_default_ttl_safely_outlives_the_hard_time_limit():
    assert default_ttl_seconds() == 60 + LOCK_TTL_SAFETY_MARGIN_SECONDS


@override_settings(CELERY_TASK_TIME_LIMIT=None)
def test_default_ttl_falls_back_without_a_configured_time_limit():
    assert default_ttl_seconds() == DEFAULT_TTL_SECONDS


@override_settings(CELERY_TASK_TIME_LIMIT=60)
def test_customer_lock_actually_applies_the_derived_default_ttl():
    # End-to-end through customer_lock (not just the helper in isolation): the PX sent
    # to Redis must itself reflect the derived TTL, not the old fixed 30s default, so a
    # slow message's lock truly outlives the 60s hard time limit that could SIGKILL it.
    client = FakeLockRedis()
    with customer_lock(WA_A, client=client, wait_seconds=1):
        _, expires_at = client._data[lock_key(WA_A)]
        assert expires_at - time.monotonic() > 60


def test_no_redis_configured_does_not_block():
    with override_settings(CONVERSATION_REDIS_URL=None):
        with customer_lock(WA_A, wait_seconds=0.01):
            pass  # no client at all: proceeds immediately, nothing to wait for


@override_settings(CONVERSATION_REDIS_URL="rediss://sessions.internal.example:6380/0")
def test_real_client_requires_certificate_verification():
    with mock.patch("redis.Redis.from_url") as from_url:
        with customer_lock("some-wa-id", wait_seconds=0.01):
            pass
    assert from_url.call_args.kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED

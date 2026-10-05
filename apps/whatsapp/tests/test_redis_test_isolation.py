"""Test-run Redis isolation: test keys never use the dev/prod prefixes."""

import uuid

import pytest
from django.conf import settings

from apps.conversations import state
from apps.conversations.state import RedisSessionStore, Session
from apps.whatsapp import dev_reset, locks, rate_limit

DEV_PREFIXES = {
    "sessions": "iva:conv:session:",
    "locks": "iva:lock:customer:",
    "rate limits": "iva:ratelimit:",
}


def test_dev_prefixes_are_what_the_reset_command_deletes(dev_key_prefixes):
    assert (dev_reset.SESSION_KEY_PREFIX, dev_reset.LOCK_KEY_PREFIX) == (
        DEV_PREFIXES["sessions"],
        DEV_PREFIXES["locks"],
    )
    assert dev_reset.RATE_LIMIT_KEY_PREFIX == DEV_PREFIXES["rate limits"]
    assert set(dev_key_prefixes) == set(DEV_PREFIXES.values())


def test_test_settings_prefix_is_not_the_dev_prefix():
    prefix = settings.IVA_TEST_REDIS_KEY_PREFIX
    assert prefix == "iva-test:"
    assert not prefix.startswith("iva:")
    assert all(not p.startswith(prefix) for p in DEV_PREFIXES.values())


def test_test_keys_never_use_a_dev_prefix():
    wa = "919000000123"
    keys = [state.session_key(wa), locks.lock_key(wa), f"{rate_limit.KEY_PREFIX}x"]
    for key in keys:
        assert key.startswith("iva-test:"), key
        for dev in DEV_PREFIXES.values():
            assert not key.startswith(dev)
            # and the reset command's patterns (built from the dev prefixes) cannot match
            assert not key.startswith(dev.rstrip("*"))


@pytest.mark.skipif(
    not getattr(settings, "CONVERSATION_REDIS_URL", None),
    reason="PENDING: CONVERSATION_REDIS_URL is not configured",
)
def test_a_real_redis_write_lands_under_the_test_prefix_only():
    store = RedisSessionStore(settings.CONVERSATION_REDIS_URL, ttl_seconds=30)
    wa = f"test-{uuid.uuid4().hex}"
    store.save(wa, Session())
    key = state.session_key(wa)
    try:
        assert key.startswith("iva-test:conv:session:")
        assert store._client.exists(key) == 1
        assert store._client.exists(DEV_PREFIXES["sessions"] + key.rsplit(":", 1)[1]) == 0
    finally:
        store.delete(wa)

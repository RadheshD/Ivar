import contextlib
import ssl
from pathlib import Path

import pytest
from django.conf import settings

import apps.whatsapp.dev_reset  # noqa: F401  (imported now: captures the dev prefixes)
from apps.conversations import state
from apps.conversations.adapters import reset_backend
from apps.whatsapp import locks, rate_limit

# The real dev/prod prefixes, captured at import (before any test patches the modules).
DEV_KEY_PREFIXES = (state.KEY_PREFIX, locks.KEY_PREFIX, rate_limit.KEY_PREFIX)
DEV_ROOT_PREFIX = "iva:"


def _test_prefix(dev_prefix: str) -> str:
    assert dev_prefix.startswith(DEV_ROOT_PREFIX), dev_prefix
    return settings.IVA_TEST_REDIS_KEY_PREFIX + dev_prefix[len(DEV_ROOT_PREFIX) :]


def _redis_client():
    url = getattr(settings, "CONVERSATION_REDIS_URL", None)
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url, socket_timeout=5, socket_connect_timeout=5, ssl_cert_reqs=ssl.CERT_REQUIRED
    )


def _sweep_test_keys() -> None:
    """Delete every key under the test prefix (never the dev one)."""
    client = _redis_client()
    if client is None:
        return
    prefix = settings.IVA_TEST_REDIS_KEY_PREFIX
    assert prefix != DEV_ROOT_PREFIX and not prefix.startswith(DEV_ROOT_PREFIX)
    with contextlib.suppress(Exception):
        for key in client.scan_iter(f"{prefix}*", count=500):
            client.delete(key)


@pytest.fixture(scope="session", autouse=True)
def _redis_test_prefix_sweep():
    """Clear leftovers of an earlier, interrupted run first, and everything at the end."""
    _sweep_test_keys()
    try:
        yield
    finally:
        _sweep_test_keys()


@pytest.fixture(autouse=True)
def _redis_test_keys(monkeypatch):
    """Route every session/lock/rate-limit key to the test prefix, remember each real
    Redis key the test touches, and delete them in teardown (even on failure)."""
    monkeypatch.setattr(state, "KEY_PREFIX", _test_prefix(state.KEY_PREFIX))
    monkeypatch.setattr(locks, "KEY_PREFIX", _test_prefix(locks.KEY_PREFIX))
    monkeypatch.setattr(rate_limit, "KEY_PREFIX", _test_prefix(rate_limit.KEY_PREFIX))

    touched: set[str] = set()
    real_call = state.RedisSessionStore._call
    real_lock_key = locks.lock_key

    def recording_call(self, operation, *args, **kwargs):
        if args and isinstance(args[0], str):
            touched.add(args[0])
        return real_call(self, operation, *args, **kwargs)

    def recording_lock_key(whatsapp_id):
        key = real_lock_key(whatsapp_id)
        touched.add(key)
        return key

    monkeypatch.setattr(state.RedisSessionStore, "_call", recording_call)
    monkeypatch.setattr(locks, "lock_key", recording_lock_key)
    try:
        yield
    finally:
        if touched:
            client = _redis_client()
            if client is not None:
                with contextlib.suppress(Exception):
                    client.delete(*touched)


@pytest.fixture(autouse=True)
def _fresh_llm_state():
    """The LLM kill switch, pause, budgets and metrics live in the shared cache: every test
    starts (and ends) with none of it set, so tests never see each other's counters."""
    from apps.ai import llm_state

    # Only ever clear an in-memory cache: clearing a real Redis would flush shared data.
    local = settings.CACHES["default"]["BACKEND"].endswith("LocMemCache")
    mine = llm_state.cache  # the object itself: a test may swap the module attribute
    if local:
        mine.clear()
    yield
    if local:
        mine.clear()


@pytest.fixture(autouse=True)
def _fresh_backend():
    """Every test starts with a new shared backend instance."""
    reset_backend()
    yield
    reset_backend()


@pytest.fixture(autouse=True)
def _never_use_the_dev_state_file(monkeypatch, tmp_path_factory):
    """Fail loudly when a test would load or save a Fake Backend state file outside pytest's
    own temp directory (the developer's saved state must never be read or written)."""
    from apps.conversations import fake_backend

    real = fake_backend._state_file_from_settings
    safe_root = Path(tmp_path_factory.getbasetemp()).resolve()

    def guarded() -> str:
        path = real()
        if path and safe_root not in Path(path).resolve().parents:
            raise AssertionError(
                "IVA_FAKE_BACKEND_STATE_FILE points outside the pytest temp directory: the "
                "test suite must never use the dev Fake Backend state file"
            )
        return path

    monkeypatch.setattr(fake_backend, "_state_file_from_settings", guarded)


@pytest.fixture(scope="session")
def dev_key_prefixes():
    """The real dev/prod key prefixes (sessions, locks, rate limits), for isolation tests."""
    return DEV_KEY_PREFIXES

"""Session state and stores. Tests marked ``redis`` need a real Redis and are skipped
(reported as pending) when CONVERSATION_REDIS_URL is not configured."""

import json
import ssl
import uuid
from unittest import mock

import pytest
from django.conf import settings
from django.test import override_settings

from apps.conversations.state import (
    InMemorySessionStore,
    RedisSessionStore,
    Session,
    SessionStore,
    SessionStoreError,
    State,
    session_key,
    store_from_settings,
    ttl_from_settings,
)

from .harness import Clock

WA = "919876543210"


def sample() -> Session:
    return Session(
        state=State.QUANTITY,
        listing={"kind": "search", "query": "oats", "offset": 0, "ids": ["ivar-oats"]},
        pending={"kind": "add", "variant_id": "ivar-oats--std"},
        idempotency_key="abc",
        refs={"r1": "long id"},
    )


def test_session_round_trip():
    session = sample()
    assert Session.from_dict(json.loads(json.dumps(session.to_dict()))) == session


@pytest.mark.parametrize(
    "data",
    [None, [], {}, {"version": 99}, {"version": 1, "state": "NOPE"}, {"version": 1, "refs": []}],
)
def test_unusable_session_data_is_no_session(data):
    assert Session.from_dict(data) is None


def test_key_never_contains_the_phone_number():
    key = session_key(WA)
    assert WA not in key
    assert key.startswith("iva:conv:session:")
    assert key == session_key(WA)


def test_in_memory_store_and_ttl():
    clock = Clock()
    store = InMemorySessionStore(ttl_seconds=60, clock=clock)
    assert isinstance(store, SessionStore)
    store.save(WA, sample())
    assert store.load(WA) == sample()
    clock.advance(59)
    assert store.load(WA) is not None
    clock.advance(2)
    assert store.load(WA) is None
    store.save(WA, sample())
    store.delete(WA)
    assert store.load(WA) is None


def test_invalid_ttl():
    with pytest.raises(ValueError):
        InMemorySessionStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        RedisSessionStore("redis://x", ttl_seconds=0)


class DictRedis:
    """Minimal stand-in for a redis client (get/set/delete), for unit tests only."""

    def __init__(self):
        self.data = {}
        self.expiry = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        self.expiry[key] = ex

    def delete(self, key):
        self.data.pop(key, None)


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError("unreachable")

        return fail


def test_redis_store_serialization_and_ttl_with_a_stub_client():
    client = DictRedis()
    store = RedisSessionStore(ttl_seconds=900, client=client)
    store.save(WA, sample())
    key = session_key(WA)
    assert client.expiry[key] == 900
    assert WA not in client.data[key]
    assert store.load(WA) == sample()
    client.data[key] = "not json"
    assert store.load(WA) is None
    store.delete(WA)
    assert store.load(WA) is None


def test_redis_errors_become_session_store_errors():
    store = RedisSessionStore(ttl_seconds=60, client=BrokenRedis())
    for call in (
        lambda: store.load(WA),
        lambda: store.save(WA, sample()),
        lambda: store.delete(WA),
    ):
        with pytest.raises(SessionStoreError):
            call()


def test_redis_store_requires_certificate_verification():
    with mock.patch("redis.Redis.from_url") as from_url:
        RedisSessionStore("rediss://sessions.internal.example:6380/0", ttl_seconds=60)
    assert from_url.call_args.kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED


def test_store_from_settings():
    with override_settings(CONVERSATION_REDIS_URL=None):
        assert isinstance(store_from_settings(), InMemorySessionStore)
    with override_settings(CONVERSATION_REDIS_URL="redis://127.0.0.1:1/0"):
        assert isinstance(store_from_settings(), RedisSessionStore)  # lazy: no connection yet


@pytest.mark.parametrize(("value", "expected"), [(900, 900), ("abc", 3600), (0, 3600)])
def test_ttl_from_settings(value, expected):
    with override_settings(CONVERSATION_SESSION_TTL_SECONDS=value):
        assert ttl_from_settings() == expected


# --------------------------------------------------------------------------- real Redis

redis_required = pytest.mark.skipif(
    not getattr(settings, "CONVERSATION_REDIS_URL", None),
    reason="PENDING: CONVERSATION_REDIS_URL is not configured (organization-provided Redis)",
)


@pytest.fixture
def real_store():
    store = RedisSessionStore(settings.CONVERSATION_REDIS_URL, ttl_seconds=30)
    whatsapp_id = f"test-{uuid.uuid4().hex}"  # a unique key; nothing else is touched
    yield store, whatsapp_id
    store.delete(whatsapp_id)


@pytest.mark.redis
@redis_required
def test_real_redis_round_trip(real_store):
    store, whatsapp_id = real_store
    store.save(whatsapp_id, sample())
    assert store.load(whatsapp_id) == sample()


@pytest.mark.redis
@redis_required
def test_real_redis_sets_a_ttl(real_store):
    store, whatsapp_id = real_store
    store.save(whatsapp_id, sample())
    ttl = store._client.ttl(session_key(whatsapp_id))
    assert 0 < ttl <= 30

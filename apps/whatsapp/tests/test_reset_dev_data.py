"""``manage.py reset_dev_data``: dry run vs. --confirm, and the safety guard.

Postgres tests run against pytest-django's isolated test database, so they're always
safe. The Redis-touching tests run against the real, shared Redis server but ONLY on
private, per-test keys under the test root ``IVA_TEST_REDIS_KEY_PREFIX`` ("iva-test:"),
and a per-test queue name. An AUTOUSE fixture applies this to EVERY test in the file
(including the Postgres ones, whose ``--confirm`` also runs the Redis step), repoints
``dev_reset``'s prefixes, queue and purge at the test values, and asserts loudly that none
of them equals or sits under the dev root ("iva:"), the real dev prefixes or the shared
"celery" queue. Celery's ``control.purge()`` empties EVERY queue, so the real call is never
made here: a test-queue-only purge stands in, and a separate mocked test pins that the
production function really calls ``control.purge()``.

Redis helpers use generous socket timeouts and a bounded retry: the remote server's first
TLS handshake is sometimes slow, and the production helpers' 2 s timeouts are right for
production but made these tests flaky.
"""

from __future__ import annotations

import ssl
import time
import uuid
import warnings
from io import StringIO

import pytest
from celery import current_app as celery_app
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.conversations.models import Conversation, ConversationMessage
from apps.whatsapp import dev_reset, services
from apps.whatsapp.parser import parse_webhook

from .fakes import payload, text_message

pytestmark = pytest.mark.django_db

redis_required = pytest.mark.skipif(
    not (dev_reset.settings.CONVERSATION_REDIS_URL and dev_reset.settings.REDIS_URL
         and dev_reset.settings.CELERY_BROKER_URL),
    reason="PENDING: CONVERSATION_REDIS_URL/REDIS_URL/CELERY_BROKER_URL not all configured",
)  # fmt: skip


def _make_conversation_row() -> None:
    [message] = parse_webhook(payload([text_message("hi", f"wamid.{uuid.uuid4().hex}")])).messages
    services.record_inbound(message)


_PRODUCTION_PURGE = dev_reset.purge_celery_queue  # captured before any test patches it
TEST_ROOT = settings.IVA_TEST_REDIS_KEY_PREFIX  # "iva-test:"
SOCKET_TIMEOUT = 15
ATTEMPTS = 4


def _is_transient(exc: BaseException) -> bool:
    """A network-level failure worth retrying (redis-py, the OS, or Celery's broker layer)."""
    import redis

    return (
        isinstance(
            exc,
            (
                redis.exceptions.TimeoutError,
                redis.exceptions.ConnectionError,
                TimeoutError,
                OSError,
            ),
        )
        or type(exc).__name__ == "OperationalError"
    )  # kombu's broker connection error


def _retry(call, attempts: int = ATTEMPTS):
    """Run ``call`` again (bounded) on a transient network error; the last error is raised."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            if not _is_transient(exc) or attempt == attempts:
                raise
            time.sleep(0.5 * attempt)


def _redis_client(url):
    """A client with long socket timeouts and a bounded connect/command retry."""
    import redis
    from redis.backoff import ExponentialBackoff
    from redis.retry import Retry

    errors = [redis.exceptions.TimeoutError, redis.exceptions.ConnectionError]
    return redis.Redis.from_url(
        url, socket_timeout=SOCKET_TIMEOUT, socket_connect_timeout=SOCKET_TIMEOUT,
        decode_responses=True, ssl_cert_reqs=ssl.CERT_REQUIRED,
        retry=Retry(ExponentialBackoff(cap=2, base=0.25), ATTEMPTS - 1), retry_on_error=errors,
    )  # fmt: skip


def assert_isolated(
    name: str, value: str, dev_root: str, dev_prefixes, test_root: str | None = None
) -> None:
    """Fail loudly unless ``value`` lives under the test root and nowhere near a dev prefix."""
    root = TEST_ROOT if test_root is None else test_root
    assert root and root != dev_root and not root.startswith(dev_root), root
    stripped = value.split(":1:", 1)[-1] if ":1:" in value else value  # Django's key wrapper
    assert stripped.startswith(root) or stripped.startswith("iva-test-"), (
        f"{name} is not under the test root: {value[:24]!r}"
    )
    assert not stripped.startswith(dev_root), f"{name} is under the dev root: {value[:24]!r}"
    assert all(not stripped.startswith(d) and not d.startswith(stripped) for d in dev_prefixes), (
        f"{name} overlaps a dev prefix: {value[:24]!r}"
    )


@pytest.fixture(autouse=True)
def isolated_redis_prefixes(monkeypatch, dev_key_prefixes):
    """Point the reset logic at private, per-test keys under the test root and a private queue,
    for EVERY test in this file. Nothing here can reach a dev prefix or the shared queue."""
    from apps.whatsapp.management.commands import reset_dev_data as command

    dev_root = "iva:"
    assert all(d.startswith(dev_root) for d in dev_key_prefixes)
    tag = uuid.uuid4().hex[:12]
    session_prefix = f"{TEST_ROOT}reset-{tag}:conv:session:"
    lock_prefix = f"{TEST_ROOT}reset-{tag}:lock:customer:"
    ratelimit_prefix = f"{TEST_ROOT}reset-{tag}:ratelimit:"
    queue_name = f"iva-test-reset-queue-{tag}"
    for name, value in (("session", session_prefix), ("lock", lock_prefix),
                        ("ratelimit", ratelimit_prefix), ("queue", queue_name)):  # fmt: skip
        assert_isolated(name, value, dev_root, dev_key_prefixes)

    monkeypatch.setattr(dev_reset, "SESSION_KEY_PREFIX", session_prefix)
    monkeypatch.setattr(dev_reset, "LOCK_KEY_PREFIX", lock_prefix)
    monkeypatch.setattr(dev_reset, "RATE_LIMIT_KEY_PREFIX", ratelimit_prefix)
    monkeypatch.setattr(dev_reset, "_queue_name", lambda: queue_name)
    monkeypatch.setattr(dev_reset, "_redis_client", lambda url: _redis_client(url) if url else None)

    real_scan = dev_reset._scan_keys

    def guarded_scan(client, pattern):
        assert_isolated("SCAN pattern", pattern.rstrip("*"), dev_root, dev_key_prefixes)
        return real_scan(client, pattern)

    monkeypatch.setattr(dev_reset, "_scan_keys", guarded_scan)

    def purge_test_queue_only() -> int:
        assert_isolated("purged queue", queue_name, dev_root, dev_key_prefixes)
        client = _redis_client(dev_reset.settings.CELERY_BROKER_URL)
        count = _retry(lambda: client.llen(queue_name))
        _retry(lambda: client.delete(queue_name))
        return count

    monkeypatch.setattr(dev_reset, "purge_celery_queue", purge_test_queue_only)
    monkeypatch.setattr(command, "purge_celery_queue", purge_test_queue_only)

    yield session_prefix, lock_prefix, ratelimit_prefix, queue_name
    if not (
        dev_reset.settings.CONVERSATION_REDIS_URL
        and dev_reset.settings.REDIS_URL
        and dev_reset.settings.CELERY_BROKER_URL
    ):
        return  # nothing was seeded without Redis
    # Defensive cleanup regardless of test outcome (dry runs delete nothing themselves).
    conv_client = _redis_client(dev_reset.settings.CONVERSATION_REDIS_URL)
    for prefix in (session_prefix, lock_prefix):
        for key in _retry(lambda p=prefix: list(conv_client.scan_iter(match=f"{p}*"))):
            _retry(lambda k=key: conv_client.delete(k))
    cache_client = _redis_client(dev_reset.settings.REDIS_URL)
    pattern = dev_reset._rate_limit_pattern()
    assert_isolated("cleanup pattern", pattern.rstrip("*"), dev_root, dev_key_prefixes)
    for key in _retry(lambda: list(cache_client.scan_iter(match=pattern))):
        _retry(lambda k=key: cache_client.delete(k))
    broker_client = _redis_client(dev_reset.settings.CELERY_BROKER_URL)
    _retry(lambda: broker_client.delete(queue_name))


def _ratelimit_wire_key(ratelimit_prefix: str) -> str:
    """The exact key Django's cache key func would produce for this prefix, per
    ``dev_reset._rate_limit_pattern()``. Written directly to real Redis, not via
    ``django.core.cache.cache``: config.settings.test forces CACHES to LocMemCache
    regardless of REDIS_URL, so the cache API never reaches real Redis under tests."""
    cache_conf = dev_reset.settings.CACHES.get("default", {})
    key_prefix = cache_conf.get("KEY_PREFIX", "")
    version = cache_conf.get("VERSION", 1)
    return f"{key_prefix}:{version}:{ratelimit_prefix}abc"


def _seed_redis(prefixes) -> None:
    session_prefix, lock_prefix, ratelimit_prefix, queue_name = prefixes
    conv_client = _redis_client(dev_reset.settings.CONVERSATION_REDIS_URL)
    _retry(lambda: conv_client.set(f"{session_prefix}abc", "1", ex=60))
    _retry(lambda: conv_client.set(f"{lock_prefix}abc", "1", ex=60))
    cache_client = _redis_client(dev_reset.settings.REDIS_URL)
    _retry(lambda: cache_client.set(_ratelimit_wire_key(ratelimit_prefix), "1", ex=60))
    _retry(lambda: _send_seed_task(queue_name))


def _send_seed_task(queue_name: str) -> None:
    with warnings.catch_warnings():
        # send_task always reaches the real broker regardless of task_always_eager
        # (that's exactly why it's used here); the warning is expected, not a problem.
        warnings.simplefilter("ignore")
        celery_app.send_task(
            "apps.whatsapp.tests.fake_task_for_reset_test", args=(), queue=queue_name
        )


# --------------------------------------------------------------------------- dry run
#
# config.settings.test leaves IVA_DEV_RESET_ALLOWED at its base.py default (False), so
# every test below that expects the command to actually run opts in explicitly.


@override_settings(IVA_DEV_RESET_ALLOWED=True)
def test_dry_run_deletes_nothing():
    _make_conversation_row()
    out = StringIO()
    call_command("reset_dev_data", stdout=out)
    output = out.getvalue()
    assert "Dry run only: nothing was deleted." in output
    assert "conversations_conversation: 1 row(s)" in output
    assert "conversations_conversationmessage: 1 row(s)" in output
    assert Conversation.objects.count() == 1
    assert ConversationMessage.objects.count() == 1


@override_settings(IVA_DEV_RESET_ALLOWED=True)
@redis_required
def test_dry_run_leaves_redis_and_celery_untouched(isolated_redis_prefixes):
    _seed_redis(isolated_redis_prefixes)
    out = StringIO()
    call_command("reset_dev_data", stdout=out)
    output = out.getvalue()
    assert "sessions: 1 key(s)" in output
    assert "customer locks: 1 key(s)" in output
    assert "rate-limit counters: 1 key(s)" in output
    assert "Celery queued tasks: 1" in output
    assert dev_reset.count_redis() == {
        "sessions": 1, "customer locks": 1, "rate-limit counters": 1,
    }  # fmt: skip
    assert dev_reset.count_celery_queue() == 1


@override_settings(IVA_DEV_RESET_ALLOWED=True)
def test_dry_run_never_prints_a_key():
    # ContentType names/table info would be an easy accidental leak vector; more to the
    # point, nothing here should ever resemble a Redis key, a pseudonym or row content.
    _make_conversation_row()
    out = StringIO()
    call_command("reset_dev_data", stdout=out)
    conversation = Conversation.objects.get()
    assert conversation.wa_id_hash not in out.getvalue()
    assert conversation.wa_id_masked not in out.getvalue()


# --------------------------------------------------------------------------- --confirm


@override_settings(IVA_DEV_RESET_ALLOWED=True)
def test_confirm_deletes_postgres_rows():
    _make_conversation_row()
    content_types_before = ContentType.objects.count()
    out = StringIO()
    call_command("reset_dev_data", "--confirm", stdout=out)
    assert "Done." in out.getvalue()
    assert Conversation.objects.count() == 0
    assert ConversationMessage.objects.count() == 0
    # The one other real table in this database is untouched.
    assert ContentType.objects.count() == content_types_before


@override_settings(IVA_DEV_RESET_ALLOWED=True)
@redis_required
def test_confirm_deletes_only_the_isolated_iva_prefixes(isolated_redis_prefixes):
    session_prefix, lock_prefix, ratelimit_prefix, queue_name = isolated_redis_prefixes
    _seed_redis(isolated_redis_prefixes)
    other_key = f"unrelated:app:{uuid.uuid4().hex}"
    conv_client = _redis_client(dev_reset.settings.CONVERSATION_REDIS_URL)
    _retry(lambda: conv_client.set(other_key, "1", ex=60))

    out = StringIO()
    call_command("reset_dev_data", "--confirm", stdout=out)

    assert dev_reset.count_redis() == {
        "sessions": 0, "customer locks": 0, "rate-limit counters": 0,
    }  # fmt: skip
    assert dev_reset.count_celery_queue() == 0
    assert _retry(lambda: conv_client.get(other_key)) == "1"  # a non-IVA key survives untouched
    _retry(lambda: conv_client.delete(other_key))


# --------------------------------------------------------------------------- safety guard


def test_refuses_unless_dev_reset_allowed():
    # No override: config.settings.test inherits IVA_DEV_RESET_ALLOWED=False from
    # base.py, same as every settings module except dev.py.
    _make_conversation_row()
    out = StringIO()
    with pytest.raises(CommandError, match="IVA_DEV_RESET_ALLOWED"):
        call_command("reset_dev_data", "--confirm", stdout=out)
    assert Conversation.objects.count() == 1  # nothing was touched


@override_settings(IVA_DEV_RESET_ALLOWED=True, IVA_BACKEND_ADAPTER="apps.somewhere.RealBackend")
def test_refuses_without_fake_backend():
    # IVA_DEV_RESET_ALLOWED is on here so this test isolates the second guard.
    _make_conversation_row()
    out = StringIO()
    with pytest.raises(CommandError, match="Fake Backend"):
        call_command("reset_dev_data", "--confirm", stdout=out)
    assert Conversation.objects.count() == 1  # nothing was touched


# --------------------------------------------------------------------------- isolation proof
#
# Every test in this file runs under the autouse ``isolated_redis_prefixes`` fixture, so
# ``reset_dev_data`` (dry run or --confirm, Postgres-only tests included) can only scan, count,
# delete or purge keys under the test root. These tests pin that and fail loudly otherwise.


def test_every_prefix_the_command_uses_is_under_the_test_root_and_not_a_dev_prefix(
    isolated_redis_prefixes, dev_key_prefixes
):
    session, lock, ratelimit, queue = isolated_redis_prefixes
    assert (dev_reset.SESSION_KEY_PREFIX, dev_reset.LOCK_KEY_PREFIX) == (session, lock)
    assert dev_reset.RATE_LIMIT_KEY_PREFIX == ratelimit and dev_reset._queue_name() == queue
    assert TEST_ROOT == "iva-test:" and not TEST_ROOT.startswith("iva:")
    for _label, _url, pattern in dev_reset._redis_prefixes():
        assert_isolated("pattern", pattern.rstrip("*"), "iva:", dev_key_prefixes)
        assert "iva:conv:session:" not in pattern and "iva:lock:customer:" not in pattern
    assert not queue.startswith("celery") and queue != "celery"


@pytest.mark.parametrize(
    "bad",
    ["iva:", "iva:conv:session:", "iva:lock:customer:", "iva:ratelimit:", "iva:llm:kill",
     "celery", ":1:iva:ratelimit:x"],
)  # fmt: skip
def test_the_guard_fails_loudly_for_any_dev_value(bad, dev_key_prefixes):
    with pytest.raises(AssertionError):
        assert_isolated("value", bad, "iva:", dev_key_prefixes)


def test_the_guard_fails_loudly_if_the_test_root_ever_equals_or_sits_under_the_dev_root(
    dev_key_prefixes,
):
    for broken in ("iva:", "iva:test:", ""):
        with pytest.raises(AssertionError):
            assert_isolated("session", f"{broken}reset-x:conv:", "iva:", dev_key_prefixes, broken)


def test_a_scan_of_a_real_dev_pattern_is_refused_before_it_reaches_redis(dev_key_prefixes):
    client = object()  # never used: the guard raises first
    for dev_pattern in ("iva:conv:session:*", "iva:lock:customer:*", ":1:iva:ratelimit:*"):
        with pytest.raises(AssertionError):
            dev_reset._scan_keys(client, dev_pattern)


def test_the_purge_used_here_never_calls_celerys_purge_all_queues(monkeypatch):
    called = []
    monkeypatch.setattr(celery_app.control, "purge", lambda: called.append(1) or 99)
    monkeypatch.setattr(dev_reset, "_redis_client", lambda url: _FakeBroker())
    assert dev_reset.purge_celery_queue() == 0
    assert called == []  # the shared queue is never purged by the test suite


def test_the_production_purge_does_use_the_supported_celery_api(monkeypatch):
    """The real function (captured before any fixture replaced it) must stay the supported
    ``control.purge()``; here it runs against a mock, never a broker."""
    calls = []
    monkeypatch.setattr(celery_app.control, "purge", lambda: calls.append(1) or 3)
    assert _PRODUCTION_PURGE() == 3 and calls == [1]


def test_the_dev_fake_backend_state_file_is_never_in_scope_under_test_settings():
    assert settings.IVA_FAKE_BACKEND_STATE_FILE == ""
    assert dev_reset.count_fake_backend_state() == {}
    assert dev_reset.clear_fake_backend_state() == {}


class _FakeBroker:
    def llen(self, name):
        return 0

    def delete(self, name):
        return 0

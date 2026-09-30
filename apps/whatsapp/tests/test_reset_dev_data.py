"""``manage.py reset_dev_data``: dry run vs. --confirm, and the safety guard.

Postgres tests run against pytest-django's isolated test database, so they're always
safe. The Redis-touching tests run against the real, shared dev Redis (per this
project's existing convention for @pytest.mark.redis tests) but are careful to operate
only on private, per-test key prefixes/queue names (monkeypatched onto
apps.whatsapp.dev_reset), never the real "iva:conv:session:"/"iva:lock:customer:"/
"iva:ratelimit:" prefixes or the shared "celery" queue -- so running this test suite
never wipes real dev-session/lock/rate-limit state. The one exception, documented on
the test itself, is Celery's control.purge(): it purges every queue Celery knows about,
with no supported way to scope it to one queue, so the --confirm test's purge assertion
is necessarily also a purge of the real shared queue (expected to be empty in normal dev
use, since nothing here runs a beat schedule and the worker/runserver are stopped before
tests, per CLAUDE.md).
"""

from __future__ import annotations

import ssl
import uuid
import warnings
from io import StringIO

import pytest
from celery import current_app as celery_app
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


def _redis_client(url):
    import redis

    return redis.Redis.from_url(
        url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )  # fmt: skip


@pytest.fixture
def isolated_redis_prefixes(monkeypatch):
    """Point the reset logic at private, per-test prefixes/queue so the test never
    touches real session/lock/rate-limit keys or the shared Celery queue."""
    tag = uuid.uuid4().hex[:12]
    session_prefix = f"iva:conv:session:test-reset-{tag}:"
    lock_prefix = f"iva:lock:customer:test-reset-{tag}:"
    ratelimit_prefix = f"iva:ratelimit:test-reset-{tag}:"
    queue_name = f"iva-test-reset-queue-{tag}"
    monkeypatch.setattr(dev_reset, "SESSION_KEY_PREFIX", session_prefix)
    monkeypatch.setattr(dev_reset, "LOCK_KEY_PREFIX", lock_prefix)
    monkeypatch.setattr(dev_reset, "RATE_LIMIT_KEY_PREFIX", ratelimit_prefix)
    monkeypatch.setattr(dev_reset, "_queue_name", lambda: queue_name)
    yield session_prefix, lock_prefix, ratelimit_prefix, queue_name
    # Defensive cleanup regardless of test outcome (dry runs delete nothing themselves).
    conv_client = _redis_client(dev_reset.settings.CONVERSATION_REDIS_URL)
    for key in conv_client.scan_iter(match=f"{session_prefix}*"):
        conv_client.delete(key)
    for key in conv_client.scan_iter(match=f"{lock_prefix}*"):
        conv_client.delete(key)
    cache_client = _redis_client(dev_reset.settings.REDIS_URL)
    for key in cache_client.scan_iter(match=dev_reset._rate_limit_pattern()):
        cache_client.delete(key)
    broker_client = _redis_client(dev_reset.settings.CELERY_BROKER_URL)
    broker_client.delete(queue_name)


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
    conv_client.set(f"{session_prefix}abc", "1", ex=60)
    conv_client.set(f"{lock_prefix}abc", "1", ex=60)
    cache_client = _redis_client(dev_reset.settings.REDIS_URL)
    cache_client.set(_ratelimit_wire_key(ratelimit_prefix), "1", ex=60)
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
    conv_client.set(other_key, "1", ex=60)

    out = StringIO()
    call_command("reset_dev_data", "--confirm", stdout=out)

    assert dev_reset.count_redis() == {
        "sessions": 0, "customer locks": 0, "rate-limit counters": 0,
    }  # fmt: skip
    assert dev_reset.count_celery_queue() == 0
    assert conv_client.get(other_key) == "1"  # a non-IVA key survives untouched
    conv_client.delete(other_key)


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

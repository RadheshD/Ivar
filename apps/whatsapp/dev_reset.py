"""Dev-only reset of IVA's own Postgres rows, Redis keys and queued Celery tasks.

Safe by construction:
- Postgres: only ``Conversation``/``ConversationMessage`` (via the ORM's own
  ``.delete()``), never raw SQL, never migrations, never any other table.
- Redis: only SCANs and deletes IVA's own known key prefixes, never ``FLUSHDB``/
  ``FLUSHALL`` and never an unscoped ``SCAN MATCH "*"`` — this matters because
  ``REDIS_URL``/``CONVERSATION_REDIS_URL``/``CELERY_BROKER_URL`` can all point at the
  same physical Redis instance, which also holds Celery's own internal bookkeeping keys.
- Celery: purged only through Celery's own supported ``control.purge()`` API.
- Fake Backend state file (``IVA_FAKE_BACKEND_STATE_FILE``): emptied, never removed.

Every count returned here is a plain integer. Nothing in this module ever returns or
prints a key, a pseudonym, message text or any row's field values.
"""

from __future__ import annotations

import ssl

from celery import current_app as celery_app
from django.conf import settings

from apps.conversations.fake_backend import clear_state_file, state_file_counts
from apps.conversations.models import Conversation, ConversationMessage
from apps.conversations.state import KEY_PREFIX as SESSION_KEY_PREFIX
from apps.whatsapp.locks import KEY_PREFIX as LOCK_KEY_PREFIX
from apps.whatsapp.rate_limit import KEY_PREFIX as RATE_LIMIT_KEY_PREFIX

# Duplicated from config/settings/prod.py's FAKE_BACKEND_PATHS, not imported: apps/ must
# never import config/ (CLAUDE.md hard rule 4, portability). Keep these two in sync.
FAKE_BACKEND_PATHS = ("apps.conversations.fake_backend", "FakeBackend")

REDIS_SCAN_COUNT = 500
REDIS_DELETE_CHUNK_SIZE = 500


class UnsafeEnvironment(Exception):
    """Refusing to run: not development, or not the Fake Backend."""


def check_safe_to_run() -> None:
    """Raise ``UnsafeEnvironment`` unless dev-reset is explicitly allowed and the Fake
    Backend is configured.

    Two independent guards, both required: ``IVA_DEV_RESET_ALLOWED`` (a plain, non-env
    setting — ``False`` everywhere except ``config/settings/dev.py`` — see
    ``config/settings/base.py``) and the Fake Backend check below. Either one refusing
    is enough to stop the command; neither depends on the other.
    """
    if not getattr(settings, "IVA_DEV_RESET_ALLOWED", False):
        raise UnsafeEnvironment("Refusing to run: IVA_DEV_RESET_ALLOWED is not enabled.")
    backend_adapter = getattr(settings, "IVA_BACKEND_ADAPTER", "") or ""
    if not any(marker in backend_adapter for marker in FAKE_BACKEND_PATHS):
        raise UnsafeEnvironment("Refusing to run: IVA_BACKEND_ADAPTER is not the Fake Backend.")


# --------------------------------------------------------------------------- Fake Backend state


def _state_file() -> str:
    return getattr(settings, "IVA_FAKE_BACKEND_STATE_FILE", "") or ""


def count_fake_backend_state() -> dict[str, int]:
    """Plain counts of what the dev state file holds; empty if none is configured."""
    path = _state_file()
    return state_file_counts(path) if path else {}


def clear_fake_backend_state() -> dict[str, int]:
    """Empty the dev state file (kept as a valid, empty state); returns what it held."""
    path = _state_file()
    if not path:
        return {}
    counts = state_file_counts(path)
    clear_state_file(path)
    return counts


# --------------------------------------------------------------------------- Postgres


def count_postgres() -> dict[str, int]:
    return {
        "conversations_conversation": Conversation.objects.count(),
        "conversations_conversationmessage": ConversationMessage.objects.count(),
    }


def delete_postgres() -> dict[str, int]:
    """Counts are taken before deleting, so they reflect exactly what was removed."""
    counts = count_postgres()
    Conversation.objects.all().delete()  # cascades to ConversationMessage
    return counts


# --------------------------------------------------------------------------- Redis


def _redis_client(url: str | None):
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url,
        socket_timeout=2,
        socket_connect_timeout=2,
        decode_responses=True,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )


def _rate_limit_pattern() -> str:
    """Django's cache key func wraps every key as ``f"{prefix}:{version}:{key}"``
    (``django.core.cache.backends.base.default_key_func``), so a bare
    ``iva:ratelimit:*`` SCAN would silently match nothing."""
    cache_conf = settings.CACHES.get("default", {})
    key_prefix = cache_conf.get("KEY_PREFIX", "")
    version = cache_conf.get("VERSION", 1)
    return f"{key_prefix}:{version}:{RATE_LIMIT_KEY_PREFIX}*"


def _redis_prefixes() -> list[tuple[str, str | None, str]]:
    """(label, Redis URL, SCAN pattern) for every known IVA Redis prefix."""
    conversation_url = getattr(settings, "CONVERSATION_REDIS_URL", None)
    cache_url = getattr(settings, "REDIS_URL", None)
    return [
        ("sessions", conversation_url, f"{SESSION_KEY_PREFIX}*"),
        ("customer locks", conversation_url, f"{LOCK_KEY_PREFIX}*"),
        ("rate-limit counters", cache_url, _rate_limit_pattern()),
    ]


def _scan_keys(client, pattern: str) -> list[str]:
    return list(client.scan_iter(match=pattern, count=REDIS_SCAN_COUNT))


def count_redis() -> dict[str, int]:
    counts: dict[str, int] = {}
    clients: dict[str | None, object] = {}
    for label, url, pattern in _redis_prefixes():
        if url not in clients:
            clients[url] = _redis_client(url)
        client = clients[url]
        counts[label] = len(_scan_keys(client, pattern)) if client else 0
    return counts


def delete_redis() -> dict[str, int]:
    """Counts are taken from the same SCAN that's about to be deleted, so they match."""
    counts: dict[str, int] = {}
    clients: dict[str | None, object] = {}
    for label, url, pattern in _redis_prefixes():
        if url not in clients:
            clients[url] = _redis_client(url)
        client = clients[url]
        if client is None:
            counts[label] = 0
            continue
        keys = _scan_keys(client, pattern)
        counts[label] = len(keys)
        for start in range(0, len(keys), REDIS_DELETE_CHUNK_SIZE):
            batch = keys[start : start + REDIS_DELETE_CHUNK_SIZE]
            if batch:
                client.delete(*batch)
    return counts


# --------------------------------------------------------------------------- Celery


def _queue_name() -> str:
    return celery_app.conf.task_default_queue or "celery"


def count_celery_queue() -> int:
    """Read-only: a plain ``LLEN`` on the broker's queue key. No custom queue is
    configured anywhere in this project, so everything routes to one queue."""
    client = _redis_client(getattr(settings, "CELERY_BROKER_URL", None))
    if client is None:
        return 0
    return client.llen(_queue_name())


def purge_celery_queue() -> int:
    """Purge via Celery's own supported control API, not a raw Redis delete."""
    return celery_app.control.purge() or 0

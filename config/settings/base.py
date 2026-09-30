"""Settings shared by every environment. HOST ONLY — not delivered with the apps."""

import os
import ssl
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# DJANGO_ENV_FILE lets tests point at a missing file so only the real environment is used.
_env_file = Path(os.environ.get("DJANGO_ENV_FILE", BASE_DIR / ".env"))
if _env_file.is_file():
    environ.Env.read_env(_env_file)

SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "host",
    "apps.conversations",
    "apps.whatsapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
TEMPLATES: list = []

# --------------------------------------------------------------------------- database
DATABASE_URL = env.str("DATABASE_URL", default="")
if DATABASE_URL:
    _db = env.db_url_config(DATABASE_URL)
    _db["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
    _db["CONN_HEALTH_CHECKS"] = True
    _db["ATOMIC_REQUESTS"] = False
    _db.setdefault("OPTIONS", {})["connect_timeout"] = env.int("DB_CONNECT_TIMEOUT", default=5)
    DATABASES = {"default": _db}
else:
    # No database configured: Django's dummy backend raises on any query, so the
    # app still starts for work that needs no database, and readiness reports "fail".
    DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- redis / cache
REDIS_URL = env.str("REDIS_URL", default="") or None
CONVERSATION_REDIS_URL = env.str("CONVERSATION_REDIS_URL", default="") or None
CONVERSATION_SESSION_TTL_SECONDS = int(
    env.str("CONVERSATION_SESSION_TTL_SECONDS", default="") or "3600"
)

# Explicit even though redis-py 8.x already defaults ssl_cert_reqs to "required": an
# unknown kwarg is silently ignored on a plain (non-TLS) connection, so this is safe to
# pass unconditionally and does not need to depend on the URL scheme.
REDIS_TLS_OPTIONS = {"ssl_cert_reqs": ssl.CERT_REQUIRED}

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": dict(REDIS_TLS_OPTIONS),
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --------------------------------------------------------------------------- i18n / time
LANGUAGE_CODE = "en"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- IVA
IVA_BACKEND_ADAPTER = env.str(
    "IVA_BACKEND_ADAPTER", default="apps.conversations.fake_backend.FakeBackend"
)

# The AI Planner page (built on the website, not this repo). Unset or non-https is
# treated as not configured: the welcome CTA and menu link are simply skipped.
IVA_PLANNER_URL = env.str("IVA_PLANNER_URL", default="")

# The full-menu page on the website (https only). Unset or non-https hides the
# "See full menu on website" main-menu option.
IVA_WEBSITE_SHOP_URL = env.str("IVA_WEBSITE_SHOP_URL", default="")

# Gate for `manage.py reset_dev_data` (apps/whatsapp/dev_reset.py): a plain, non-env
# hardcoded switch, not read from .env, so it can never be flipped on by a stray
# environment variable. Only config/settings/dev.py turns it on. The reset command's
# second guard (the Fake Backend check) stays regardless, as independent protection.
IVA_DEV_RESET_ALLOWED = False

# Keys the HMAC pseudonyms of WhatsApp IDs (sessions, conversation records). Separate from
# SECRET_KEY; must be carried over unchanged into the organization's project.
IVA_PSEUDONYM_KEY = env.str("IVA_PSEUDONYM_KEY", default="")

# --------------------------------------------------------------------------- hardening (S5)
# Messages per customer per minute before the "please slow down" reply (apps.whatsapp.rate_limit).
IVA_RATE_LIMIT_PER_MINUTE = env.int("IVA_RATE_LIMIT_PER_MINUTE", default=20)
# How long a message waits for another message from the same customer to finish
# (apps.whatsapp.locks). The lock's own TTL, a separate safety net for a crashed
# holder, is fixed at locks.DEFAULT_TTL_SECONDS.
IVA_CUSTOMER_LOCK_WAIT_SECONDS = env.int("IVA_CUSTOMER_LOCK_WAIT_SECONDS", default=30)
# Development only: the webhook runs apps.whatsapp.tasks.process_inbound_inline()
# synchronously instead of enqueuing a Celery task, so no worker or broker is needed.
# Same claim/lock/rate-limit/at-most-once code path as the worker; only the lock-retry
# mechanism differs (a blocking sleep instead of Celery's self.retry()). Production
# settings refuse to start with this on (config/settings/prod.py).
IVA_INLINE_PROCESSING = env.bool("IVA_INLINE_PROCESSING", default=False)

# --------------------------------------------------------------------------- WhatsApp (S2)
WHATSAPP_ACCESS_TOKEN = env.str("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_APP_SECRET = env.str("WHATSAPP_APP_SECRET", default="")
WHATSAPP_VERIFY_TOKEN = env.str("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env.str("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_API_VERSION = env.str("WHATSAPP_API_VERSION", default="")
WHATSAPP_GRAPH_BASE_URL = (
    env.str("WHATSAPP_GRAPH_BASE_URL", default="") or "https://graph.facebook.com"
)
# Conversation message metadata is kept this long; message text is never stored.
IVA_MESSAGE_RETENTION_DAYS = int(env.str("IVA_MESSAGE_RETENTION_DAYS", default="") or "90")

# Base URL the Fake Backend joins with a product's relative image path to build
# ProductSummary/ProductDetail.image_url. Unset or non-https means no images (the
# Fake Backend itself re-checks the scheme; this is not otherwise validated here).
IVA_PRODUCT_IMAGE_BASE_URL = env.str("IVA_PRODUCT_IMAGE_BASE_URL", default="")

# --------------------------------------------------------------------------- LLM (intent layer)
# Empty LLM_PROVIDER = no LLM fallback; the intent layer runs on rules and fuzzy matching.
LLM_PROVIDER = env.str("LLM_PROVIDER", default="")
LLM_API_KEY = env.str("LLM_API_KEY", default="")
LLM_MODEL = env.str("LLM_MODEL", default="")
LLM_TIMEOUT_SECONDS = env.str("LLM_TIMEOUT_SECONDS", default="") or "5"

# --------------------------------------------------------------------------- celery
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="") or None
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 30
CELERY_TASK_TIME_LIMIT = 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = TIME_ZONE
# Kombu's redis transport issues a fresh BRPOP every "polling_interval" seconds while
# idle (default: 1s -> ~86,400 commands/day from one idle worker alone). BRPOP is a
# blocking read that returns the moment a message arrives regardless of this value, so
# raising it cuts idle command volume without adding reply latency (verified against
# kombu/transport/redis.py: Transport.__init__ maps this straight onto brpop_timeout,
# and with a real worker: pickup time after a task is enqueued is unaffected).
# "health_check_interval" is the same idea for the idle-connection keepalive PING:
# raised from kombu's 25s default, but only as far as empirically confirmed safe
# against Upstash (connection survived 130s of true silence in testing) with a wide
# margin, since Upstash's own idle-disconnect threshold isn't documented.
# Both default to the lowest-usage values that were actually measured (see CLAUDE.md's
# 2026-09-25 Upstash free-tier tuning note); a paid/less-constrained tier can lower
# them again via the env vars below with no code change.
# visibility_timeout is deliberately left at its default: the claim guard in
# apps.whatsapp.services makes a late redelivery harmless, so there's no need to make
# Celery's own crash-recovery faster. The unacked-message restore scan it drives
# (kombu's MultiChannelPoller.maybe_restore_messages, in kombu/transport/redis.py) is
# now the dominant idle-command source once gossip/mingle/heartbeat and polling are
# tuned down (measured via MONITOR: fires roughly every 20-24s, not the ~100s a plain
# reading of its 10s-schedule/10-call-skip source suggests -- real behaviour differs
# from that static analysis for a reason not fully pinned down). It is NOT gated by
# CELERY_TASK_ACKS_LATE, confirmed by measuring with acks_late=False against the real
# broker: the scan's cadence and command volume were unchanged (~28 commands/130s vs.
# ~31/130s with acks_late=True). It is registered unconditionally in Kombu's
# Transport.register_with_event_loop() for any channel run through the async
# event-loop consumer at all -- a core part of Kombu's reliable-delivery machinery for
# this transport, not a Celery-level setting. It is not exposed via any Celery/Kombu
# setting (only its batch size, unacked_restore_limit, is), so it is left alone rather
# than monkeypatching a third-party library's transport internals.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "polling_interval": env.int("IVA_CELERY_POLLING_INTERVAL_SECONDS", default=30),
    "health_check_interval": env.int("IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS", default=60),
}

# A "rediss://" broker without this raises no error but silently drops to CERT_NONE
# (kombu.connection logs "defaulting to insecure SSL behaviour"). Only set it for a
# secure scheme: kombu forces every connection through its SSL connection class
# whenever broker_use_ssl is set at all, which would break a plain "redis://" broker.
if CELERY_BROKER_URL and CELERY_BROKER_URL.startswith("rediss://"):
    CELERY_BROKER_USE_SSL = dict(REDIS_TLS_OPTIONS)
# Keep Django's LOGGING (with the masking filter) instead of Celery's own handlers.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# --------------------------------------------------------------------------- logging
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"mask": {"()": "apps.iva_common.logging.MaskingFilter"}},
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["mask"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.server": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "celery": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

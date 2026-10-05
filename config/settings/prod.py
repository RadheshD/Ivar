from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import (
    ALLOWED_HOSTS,
    CELERY_BROKER_URL,
    CONVERSATION_REDIS_URL,
    CSRF_TRUSTED_ORIGINS,
    DATABASE_URL,
    IVA_FAKE_BACKEND_STATE_FILE,
    IVA_INLINE_PROCESSING,
    IVA_PSEUDONYM_KEY,
    REDIS_URL,
    SECRET_KEY,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_API_VERSION,
    WHATSAPP_APP_SECRET,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_VERIFY_TOKEN,
    env,
)

FAKE_BACKEND_PATHS = ("apps.conversations.fake_backend", "FakeBackend")


def validate_production(
    *,
    secret_key: str,
    database_url: str,
    redis_url: str | None,
    celery_broker_url: str | None,
    conversation_redis_url: str | None,
    allowed_hosts: list[str],
    csrf_trusted_origins: list[str],
    backend_adapter: str | None,
    whatsapp: dict[str, str] | None = None,
    pseudonym_key: str = "",
    inline_processing: bool = False,
    fake_backend_state_file: str = "",
) -> None:
    """Refuse to start production with unsafe or incomplete configuration."""
    required = {
        "DJANGO_SECRET_KEY": secret_key,
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": celery_broker_url,
        # Sessions must be shared by every worker process; the in-memory store is not.
        "CONVERSATION_REDIS_URL": conversation_redis_url,
        "DJANGO_ALLOWED_HOSTS": allowed_hosts,
        "IVA_BACKEND_ADAPTER": backend_adapter,
        **(whatsapp or {}),
        "IVA_PSEUDONYM_KEY": pseudonym_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"Production requires: {', '.join(missing)}.")
    for host in allowed_hosts:
        if "*" in host or host.startswith("."):
            raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must not contain wildcards.")
    for origin in csrf_trusted_origins:
        if "*" in origin:
            raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must not contain wildcards.")
    if len(pseudonym_key) < 32:
        raise ImproperlyConfigured("IVA_PSEUDONYM_KEY must be at least 32 characters.")
    if pseudonym_key == secret_key:
        raise ImproperlyConfigured("IVA_PSEUDONYM_KEY must differ from DJANGO_SECRET_KEY.")
    if any(marker in backend_adapter for marker in FAKE_BACKEND_PATHS):
        raise ImproperlyConfigured("The Fake Backend must not be used in production.")
    if fake_backend_state_file:
        raise ImproperlyConfigured(
            "IVA_FAKE_BACKEND_STATE_FILE must not be set in production; it is development-only."
        )
    if inline_processing:
        raise ImproperlyConfigured(
            "IVA_INLINE_PROCESSING must not be used in production; it is development-only."
        )


# Production never falls back to the base default adapter: read it again with no default.
IVA_BACKEND_ADAPTER = env.str("IVA_BACKEND_ADAPTER", default="")

validate_production(
    secret_key=SECRET_KEY,
    database_url=DATABASE_URL,
    redis_url=REDIS_URL,
    celery_broker_url=CELERY_BROKER_URL,
    conversation_redis_url=CONVERSATION_REDIS_URL,
    allowed_hosts=ALLOWED_HOSTS,
    csrf_trusted_origins=CSRF_TRUSTED_ORIGINS,
    backend_adapter=IVA_BACKEND_ADAPTER,
    whatsapp={
        "WHATSAPP_ACCESS_TOKEN": WHATSAPP_ACCESS_TOKEN,
        "WHATSAPP_APP_SECRET": WHATSAPP_APP_SECRET,
        "WHATSAPP_VERIFY_TOKEN": WHATSAPP_VERIFY_TOKEN,
        "WHATSAPP_PHONE_NUMBER_ID": WHATSAPP_PHONE_NUMBER_ID,
        "WHATSAPP_API_VERSION": WHATSAPP_API_VERSION,
    },
    pseudonym_key=IVA_PSEUDONYM_KEY,
    inline_processing=IVA_INLINE_PROCESSING,
    fake_backend_state_file=IVA_FAKE_BACKEND_STATE_FILE,
)

DEBUG = False

SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Deliberately off (Django's system check flags this as security.W021): submitting a
# domain to the browser preload list is effectively permanent (removal can take months
# and depends on browser vendors), and it applies to every subdomain via
# SECURE_HSTS_INCLUDE_SUBDOMAINS. That should be a separate, deliberate step the
# organization takes once this domain's HTTPS setup has been stable in production for a
# while, not something this codebase turns on by default on day one.
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

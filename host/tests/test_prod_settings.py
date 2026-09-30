"""Production settings refuse unsafe configuration.

Each case imports ``config.settings.prod`` in a fresh interpreter with a controlled
environment and no ``.env`` file, so nothing leaks between cases.
"""

import json
import os
import ssl
import subprocess
import sys

import pytest
from django.conf import settings
from django.test import Client, override_settings

# Varied on purpose: Django's deploy check (security.W009) flags a secret key with
# fewer than 5 unique characters, which a simple "x" * 60 placeholder would trip.
_SECRET_KEY_PLACEHOLDER = "".join(f"{i % 10}{chr(97 + i % 26)}" for i in range(30))

VALID_ENV = {
    "DJANGO_SECRET_KEY": _SECRET_KEY_PLACEHOLDER,
    "DATABASE_URL": "postgres://user:pw@127.0.0.1:5432/iva",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "CELERY_BROKER_URL": "redis://127.0.0.1:6379/1",
    "CONVERSATION_REDIS_URL": "redis://127.0.0.1:6379/2",
    "DJANGO_ALLOWED_HOSTS": "iva.example.com",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://iva.example.com",
    "IVA_BACKEND_ADAPTER": "somewhere.real.RealBackend",
    # Placeholders only: nothing ever calls Meta in tests.
    "WHATSAPP_ACCESS_TOKEN": "placeholder-token",
    "WHATSAPP_APP_SECRET": "placeholder-secret",
    "WHATSAPP_VERIFY_TOKEN": "placeholder-verify",
    "WHATSAPP_PHONE_NUMBER_ID": "100000000000001",
    "WHATSAPP_API_VERSION": "v21.0",
    "IVA_PSEUDONYM_KEY": "p" * 48,
    # Explicitly controlled (not left to leak from the real environment): the dev .env
    # sets this true for local development, and read_env() puts it in os.environ for
    # every test in this process, so every subprocess here must override it back to a
    # safe value rather than inherit whatever the developer's own .env happens to have.
    "IVA_INLINE_PROCESSING": "false",
}

CONTROLLED = set(VALID_ENV) | {"DJANGO_DEBUG", "DJANGO_SETTINGS_MODULE", "DJANGO_ENV_FILE"}

PROBE = """
import json
from django.conf import settings
print(json.dumps({
    "DEBUG": settings.DEBUG,
    "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
    "SECURE_REDIRECT_EXEMPT": settings.SECURE_REDIRECT_EXEMPT,
    "SECURE_HSTS_SECONDS": settings.SECURE_HSTS_SECONDS,
    "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
    "SECURE_CONTENT_TYPE_NOSNIFF": settings.SECURE_CONTENT_TYPE_NOSNIFF,
    "X_FRAME_OPTIONS": settings.X_FRAME_OPTIONS,
    "SECURE_PROXY_SSL_HEADER": list(settings.SECURE_PROXY_SSL_HEADER),
    "CELERY_BROKER_USE_SSL": (
        {k: int(v) for k, v in settings.CELERY_BROKER_USE_SSL.items()}
        if getattr(settings, "CELERY_BROKER_USE_SSL", None)
        else None
    ),
    "CACHE_SSL_CERT_REQS": (
        int(settings.CACHES["default"].get("OPTIONS", {}).get("ssl_cert_reqs"))
        if settings.CACHES["default"].get("OPTIONS", {}).get("ssl_cert_reqs") is not None
        else None
    ),
}))
"""


def run_prod(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in CONTROLLED}
    env.update(VALID_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.prod"
    env["DJANGO_ENV_FILE"] = os.path.join(str(settings.BASE_DIR), "does-not-exist.env")
    return subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def assert_refused(result, fragment):
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert fragment in result.stderr


@pytest.fixture(scope="module")
def prod_values():
    result = run_prod(DJANGO_DEBUG="true")  # DEBUG must stay False even if asked for.
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_valid_prod_config_loads_with_debug_false(prod_values):
    assert prod_values["DEBUG"] is False


def test_cache_always_requires_certificate_verification(prod_values):
    # Unconditional: an unrecognised kwarg is silently ignored on a plain connection,
    # so this holds regardless of whether REDIS_URL is "redis://" or "rediss://".
    assert prod_values["CACHE_SSL_CERT_REQS"] == ssl.CERT_REQUIRED


def test_broker_use_ssl_is_untouched_for_a_plain_broker(prod_values):
    # VALID_ENV's CELERY_BROKER_URL is a plain "redis://"; forcing SSL onto it would
    # break the connection, so CELERY_BROKER_USE_SSL must stay unset.
    assert prod_values["CELERY_BROKER_USE_SSL"] is None


def test_rediss_broker_requires_certificate_verification():
    result = run_prod(
        REDIS_URL="rediss://cache.example.com:6380/0",
        CELERY_BROKER_URL="rediss://broker.example.com:6380/1",
        CONVERSATION_REDIS_URL="rediss://sessions.example.com:6380/2",
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout.strip().splitlines()[-1])
    assert values["CELERY_BROKER_USE_SSL"] == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    assert values["CACHE_SSL_CERT_REQS"] == ssl.CERT_REQUIRED


def test_prod_security_headers(prod_values):
    assert prod_values["SECURE_SSL_REDIRECT"] is True
    assert prod_values["SECURE_HSTS_SECONDS"] >= 31536000
    assert prod_values["SESSION_COOKIE_SECURE"] is True
    assert prod_values["CSRF_COOKIE_SECURE"] is True
    assert prod_values["SECURE_CONTENT_TYPE_NOSNIFF"] is True
    assert prod_values["X_FRAME_OPTIONS"] == "DENY"
    assert prod_values["SECURE_PROXY_SSL_HEADER"] == ["HTTP_X_FORWARDED_PROTO", "https"]


@pytest.mark.parametrize(
    "missing",
    [
        "DJANGO_SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CONVERSATION_REDIS_URL",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_API_VERSION",
        "IVA_PSEUDONYM_KEY",
    ],
)
def test_missing_required_value_is_refused(missing):
    assert_refused(run_prod(**{missing: None}), missing)


def test_short_pseudonym_key_is_refused():
    assert_refused(run_prod(IVA_PSEUDONYM_KEY="short"), "IVA_PSEUDONYM_KEY")


def test_pseudonym_key_must_differ_from_the_secret_key():
    assert_refused(run_prod(IVA_PSEUDONYM_KEY=_SECRET_KEY_PLACEHOLDER), "must differ")


def test_empty_secret_key_is_refused():
    assert_refused(run_prod(DJANGO_SECRET_KEY=""), "DJANGO_SECRET_KEY")


@pytest.mark.parametrize("hosts", ["*", "iva.example.com,*", "*.example.com", ".example.com"])
def test_wildcard_host_is_refused(hosts):
    assert_refused(run_prod(DJANGO_ALLOWED_HOSTS=hosts), "wildcards")


def test_wildcard_origin_is_refused():
    assert_refused(run_prod(DJANGO_CSRF_TRUSTED_ORIGINS="https://*.example.com"), "wildcards")


def test_deploy_check_has_no_unexplained_warnings():
    env = {k: v for k, v in os.environ.items() if k not in CONTROLLED}
    env.update(VALID_ENV)
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.prod"
    env["DJANGO_ENV_FILE"] = os.path.join(str(settings.BASE_DIR), "does-not-exist.env")
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "manage.py", "check", "--deploy"],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # security.W021 (HSTS preload) is the one deliberate, explained exception: see the
    # comment on SECURE_HSTS_PRELOAD in config/settings/prod.py and ARCHITECTURE.md §10.
    assert "security.W021" in result.stderr
    other_warnings = [
        line
        for line in result.stderr.splitlines()
        if line.startswith("?:") and "security.W021" not in line
    ]
    assert other_warnings == []


@pytest.mark.parametrize("adapter", [None, ""])
def test_unset_adapter_is_refused(adapter):
    assert_refused(run_prod(IVA_BACKEND_ADAPTER=adapter), "IVA_BACKEND_ADAPTER")


def test_fake_backend_is_refused():
    result = run_prod(IVA_BACKEND_ADAPTER="apps.conversations.fake_backend.FakeBackend")
    assert_refused(result, "Fake Backend")


def test_inline_processing_is_refused():
    assert_refused(run_prod(IVA_INLINE_PROCESSING="true"), "IVA_INLINE_PROCESSING")


def test_https_redirect_exempts_only_health(prod_values):
    exempt = prod_values["SECURE_REDIRECT_EXEMPT"]
    assert exempt == [r"^health/"]
    with override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=exempt):
        client = Client()
        assert client.get("/health/live/").status_code == 200
        for path in ("/", "/webhooks/whatsapp/", "/healthz/", "/x/health/live/"):
            response = client.get(path)
            assert response.status_code == 301, path
            assert response["Location"].startswith("https://")

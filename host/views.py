"""Health checks and JSON error handlers for the development host."""

import logging
import ssl

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

OK, FAIL, NOT_CONFIGURED = "ok", "fail", "not_configured"


def _check_database() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        logger.error("Readiness: database check failed (%s).", type(exc).__name__)
        return FAIL
    return OK


def _check_redis() -> str:
    url = getattr(settings, "REDIS_URL", None)
    if not url:
        return NOT_CONFIGURED
    try:
        client = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2, ssl_cert_reqs=ssl.CERT_REQUIRED
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:
        logger.error("Readiness: redis check failed (%s).", type(exc).__name__)
        return FAIL
    return OK


@require_GET
def live(request):
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request):
    checks = {"database": _check_database(), "redis": _check_redis()}
    healthy = all(v in (OK, NOT_CONFIGURED) for v in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "fail", "checks": checks},
        status=200 if healthy else 503,
    )


def not_found(request, exception=None):
    return JsonResponse({"error": "not_found"}, status=404)


def server_error(request):
    return JsonResponse({"error": "server_error"}, status=500)

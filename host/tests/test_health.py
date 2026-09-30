import logging
import ssl
from unittest import mock

import pytest
import redis
from django.db import OperationalError
from django.test import override_settings

SECRET_DETAIL = "db.internal.example:5432 password=hunter2 PostgreSQL 16.4"


def test_live_returns_ok(client):
    response = client.get("/health/live/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_get_only(client):
    assert client.post("/health/live/").status_code == 405
    assert client.post("/health/ready/").status_code == 405


@pytest.mark.django_db
@override_settings(REDIS_URL=None)
def test_ready_ok_with_real_database(client):
    response = client.get("/health/ready/")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "not_configured"},
    }


@override_settings(REDIS_URL=None)
def test_ready_503_when_database_fails(client, caplog):
    with mock.patch("host.views.connection.cursor", side_effect=OperationalError(SECRET_DETAIL)):
        with caplog.at_level(logging.ERROR, logger="host.views"):
            response = client.get("/health/ready/")
    assert response.status_code == 503
    assert response.json() == {
        "status": "fail",
        "checks": {"database": "fail", "redis": "not_configured"},
    }
    assert SECRET_DETAIL not in response.content.decode()
    assert "OperationalError" in caplog.text
    assert "hunter2" not in caplog.text


@override_settings(REDIS_URL="redis://redis.internal.example:6379/0")
def test_ready_503_when_redis_fails(client, caplog):
    fake = mock.Mock()
    fake.ping.side_effect = redis.ConnectionError("Error connecting to redis.internal.example")
    with (
        mock.patch("host.views._check_database", return_value="ok"),
        mock.patch("host.views.redis.Redis.from_url", return_value=fake),
        caplog.at_level(logging.ERROR, logger="host.views"),
    ):
        response = client.get("/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "fail", "checks": {"database": "ok", "redis": "fail"}}
    body = response.content.decode()
    assert "redis.internal" not in body and "Error" not in body
    assert "ConnectionError" in caplog.text
    fake.close.assert_called_once()


@override_settings(REDIS_URL="redis://localhost:6379/0")
def test_ready_ok_when_redis_pings(client):
    fake = mock.Mock()
    fake.ping.return_value = True
    with (
        mock.patch("host.views._check_database", return_value="ok"),
        mock.patch("host.views.redis.Redis.from_url", return_value=fake),
    ):
        response = client.get("/health/ready/")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}


@override_settings(REDIS_URL="rediss://redis.internal.example:6380/0")
def test_redis_health_check_requires_certificate_verification(client):
    fake = mock.Mock()
    fake.ping.return_value = True
    with mock.patch("host.views.redis.Redis.from_url", return_value=fake) as from_url:
        client.get("/health/ready/")
    assert from_url.call_args.kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED


def test_unknown_url_is_json_404(client):
    response = client.get("/does-not-exist/")
    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"

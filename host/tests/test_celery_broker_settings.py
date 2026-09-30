"""``CELERY_BROKER_TRANSPORT_OPTIONS`` must actually reach Kombu's redis transport, or
tuning it does nothing to real idle command volume.

``polling_interval`` is verified end-to-end with no network connection: Kombu's redis
``Transport`` resolves it onto ``brpop_timeout`` at construction time, before anything
touches a socket. ``health_check_interval`` is a ``Channel``-level option instead, and
building a ``Channel`` always opens a real connection (``Channel.__init__`` pings
immediately) — there is no way to observe it without one. It is confirmed by source
instead: ``redis.Channel.from_transport_options`` (kombu/transport/redis.py) lists
``'health_check_interval'`` alongside ``'visibility_timeout'`` and ``'socket_timeout'``,
the same declarative mechanism this project already relies on for those.
"""

import kombu
from django.conf import settings


def test_polling_interval_reaches_kombu_as_brpop_timeout():
    options = settings.CELERY_BROKER_TRANSPORT_OPTIONS
    assert options.get("polling_interval"), "polling_interval must be set and truthy"

    conn = kombu.Connection("redis://127.0.0.1:6379/0", transport_options=options)
    transport = conn.transport
    assert transport.polling_interval == options["polling_interval"]
    assert transport.brpop_timeout == options["polling_interval"]


def test_health_check_interval_is_a_declared_redis_channel_option():
    from kombu.transport.redis import Channel

    options = settings.CELERY_BROKER_TRANSPORT_OPTIONS
    assert options.get("health_check_interval"), "health_check_interval must be set and truthy"
    assert "health_check_interval" in Channel.from_transport_options


def test_low_usage_defaults_match_the_measured_upstash_tuning():
    # Pinned so a future change is deliberate, not an accidental import-order or env
    # default drift: these are the values CLAUDE.md's tuning note was measured against.
    assert settings.CELERY_BROKER_TRANSPORT_OPTIONS == {
        "polling_interval": 30,
        "health_check_interval": 60,
    }

"""Keep a buttons message behind the large message it follows.

The Cloud API accepts a send request as soon as it is validated, then prepares the
message (fetching a carousel's or card's images) on its own schedule. A light buttons
message sent right after a carousel can therefore overtake it, even though we only send
it after the carousel's send call has returned. WhatsApp's own status callback for the
carousel ("sent") says it is out of the preparation step, so the follow-up waits for
that callback, up to a timeout, and is then sent regardless -- never lost.

The webhook (any process) records each status callback with ``note_status``; the worker
polls for it with ``wait_for_status``. Both use ``settings.CACHES`` (Redis in production,
local memory without it), keyed by a pseudonym of the message ID. Every failure path
fails open: no wait, the follow-up goes out immediately, as it did before this module.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache

from apps.iva_common.pseudonym import pseudonymize

logger = logging.getLogger(__name__)

KEY_PREFIX = "iva:wastatus:"
_TTL_SECONDS = 120
POLL_INTERVAL_SECONDS = 0.5
DEFAULT_WAIT_SECONDS = 5.0
# Statuses that end the wait, per ``IVA_ORDER_WAIT_STATUS`` ("sent" or "delivered").
# Callbacks can arrive out of order, so a later status also satisfies an earlier one.
_SATISFIES = {
    "sent": {"sent", "delivered", "read"},
    "delivered": {"delivered", "read"},
}


def _key(wa_message_id: str) -> str:
    return f"{KEY_PREFIX}{pseudonymize(wa_message_id)}"


def wait_seconds() -> float:
    try:
        value = float(getattr(settings, "IVA_ORDER_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS
    return max(value, 0.0)


def _wanted() -> set[str]:
    return _SATISFIES.get(getattr(settings, "IVA_ORDER_WAIT_STATUS", "sent"), _SATISFIES["sent"])


def note_status(wa_message_id: str, status: str) -> None:
    """Called by the webhook for every outbound status callback (never raises)."""
    if status not in _SATISFIES["sent"]:
        return
    try:
        cache.set(_key(wa_message_id), status, timeout=_TTL_SECONDS)
    except Exception as exc:
        logger.warning("ordering: could not note status error=%s", type(exc).__name__)


def wait_for_status(
    wa_message_id: str,
    *,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until the message's status callback arrived (True) or ``timeout`` passed
    (False). ``timeout`` 0 (or ``IVA_ORDER_WAIT_SECONDS=0``) disables waiting."""
    limit = wait_seconds() if timeout is None else timeout
    if limit <= 0:
        return False
    wanted = _wanted()
    deadline = clock() + limit
    key = _key(wa_message_id)
    while True:
        try:
            if cache.get(key) in wanted:
                return True
        except Exception as exc:
            logger.warning("ordering: status check failed error=%s", type(exc).__name__)
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(POLL_INTERVAL_SECONDS, remaining))

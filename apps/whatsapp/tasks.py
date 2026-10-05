"""Process one inbound message (engine -> replies -> Cloud API).

At most once: the message is claimed in the database before the engine runs, so a
redelivered task cannot add to a cart or place an order twice. Task arguments are shown
masked in Celery's logs and events (``kwargsrepr``).

Two entry points share the same processing core (``_process_once``), so claim/lock/
rate-limit/at-most-once behaviour is identical either way:
- ``process_inbound``: the normal Celery task, used in production.
- ``process_inbound_inline``: a synchronous equivalent for
  ``settings.IVA_INLINE_PROCESSING`` (development only, no worker/broker needed),
  called directly from the webhook view.

A per-customer lock timeout is the one exception that retries: it fires before the
engine ever runs (raised while still waiting to acquire the lock, never from inside
it), so nothing has happened yet and retrying is safe. Celery's ``self.retry()`` is
unavailable inline, so ``process_inbound_inline`` retries with a plain blocking sleep
instead, using the same ``MAX_LOCK_RETRIES`` / ``LOCK_RETRY_COUNTDOWN_SECONDS``.
"""

from __future__ import annotations

import logging
import time

from celery import shared_task
from django.conf import settings

from apps.conversations.channel import Inbound
from apps.conversations.engine import get_engine
from apps.conversations.models import ConversationMessage
from apps.iva_common.logging import mask_phone

from . import ordering, rate_limit, services
from .client import client_from_settings
from .delivery import send_replies
from .locks import CustomerLockTimeout, customer_lock
from .parser import InboundMessage

logger = logging.getLogger(__name__)

MAX_LOCK_RETRIES = 3
LOCK_RETRY_COUNTDOWN_SECONDS = 2


def enqueue(message_pk: int, message: InboundMessage) -> None:
    kwargs = {
        "message_pk": message_pk,
        "wa_id": message.wa_id,
        "text": message.text,
        "reply_id": message.reply_id,
        "reply_title": message.reply_title,
        "profile_name": message.profile_name,
    }
    shown = {"message_pk": message_pk, "wa_id": mask_phone(message.wa_id)}
    process_inbound.apply_async(kwargs=kwargs, kwargsrepr=repr(shown))


def _replies(
    wa_id: str,
    text: str | None,
    reply_id: str | None,
    reply_title: str | None,
    profile_name: str | None,
):
    """The rate limit is checked first: an over-the-cap message never reaches the
    engine or touches the session, it only ever gets the one-time "slow down" notice
    or (once already warned this window) no reply at all."""
    limited = rate_limit.register_message(wa_id)
    if not limited.allowed:
        return get_engine().rate_limited_reply(wa_id) if limited.warn else []
    inbound = Inbound(
        text=text, reply_id=reply_id, reply_title=reply_title, profile_name=profile_name
    )
    return get_engine().handle(wa_id, inbound)


def _process_once(
    message_pk: int,
    wa_id: str,
    text: str | None,
    reply_id: str | None,
    reply_title: str | None,
    profile_name: str | None = None,
) -> None:
    """One processing attempt, shared by the worker task and inline mode.

    Raises ``CustomerLockTimeout`` if the per-customer lock times out; the caller
    decides whether/how to retry (Celery requeue vs. a blocking sleep). Otherwise
    returns normally once claimed-and-processed, already-taken, or outside the reply
    window — in every case the message is either completed or left for
    ``report_stalled_messages``/``report_lock_exhausted`` to account for, never silently
    dropped.
    """
    if not services.claim_for_processing(message_pk):
        logger.info("process_inbound: message %s already taken", message_pk)
        return
    conversation = (
        ConversationMessage.objects.select_related("conversation").get(pk=message_pk).conversation
    )
    if not services.can_reply(conversation):
        logger.warning("process_inbound: outside the 24h window to=%s", mask_phone(wa_id))
        services.complete_processing(message_pk)
        return
    client = client_from_settings()
    # Serializes this customer's messages: while a previous one is still inside this
    # block (session load -> engine -> session save), any other message from the same
    # customer waits its turn here instead of racing it.
    with customer_lock(wa_id, wait_seconds=settings.IVA_CUSTOMER_LOCK_WAIT_SECONDS):
        replies = _replies(wa_id, text, reply_id, reply_title, profile_name)
        for sent in send_replies(
            wa_id, replies, client=client, wait_for_status=ordering.wait_for_status
        ):
            services.record_outbound(
                conversation,
                message_type=sent.message_type,
                template_key=sent.template_key,
                wa_message_id=sent.wa_message_id,
                error_code=sent.error.code if sent.error else None,
            )
    # Not reached if the engine or a send crashes: the message then shows up in
    # report_stalled_messages after STALL_AFTER.
    services.complete_processing(message_pk)


@shared_task(bind=True, name="whatsapp.process_inbound", ignore_result=True)
def process_inbound(
    self,
    message_pk: int,
    wa_id: str,
    text: str | None = None,
    reply_id: str | None = None,
    reply_title: str | None = None,
    profile_name: str | None = None,
) -> None:
    try:
        _process_once(message_pk, wa_id, text, reply_id, reply_title, profile_name)
    except CustomerLockTimeout as exc:
        # Raised before the ``with`` block's body ever starts, so the engine has not
        # run: releasing the claim and re-enqueuing is safe, at-most-once still holds
        # (claim_for_processing is the only thing that ever lets it run).
        if self.request.retries < MAX_LOCK_RETRIES:
            services.release_claim(message_pk)
            logger.warning(
                "process_inbound: per-customer lock timed out to=%s; retrying (%d/%d)",
                mask_phone(wa_id), self.request.retries + 1, MAX_LOCK_RETRIES,
            )  # fmt: skip
            raise self.retry(
                countdown=LOCK_RETRY_COUNTDOWN_SECONDS, max_retries=MAX_LOCK_RETRIES
            ) from exc
        # Retries exhausted: leave the claim in place (never reclaimable, same as a
        # crash) and report it now rather than waiting for the STALL_AFTER sweep.
        services.report_lock_exhausted(message_pk, wa_id)


def process_inbound_inline(
    message_pk: int,
    wa_id: str,
    text: str | None = None,
    reply_id: str | None = None,
    reply_title: str | None = None,
    profile_name: str | None = None,
) -> None:
    """Synchronous equivalent of ``process_inbound`` for ``IVA_INLINE_PROCESSING``
    (development only): runs the same ``_process_once`` core directly in the webhook
    request, no Celery worker or broker involved. A per-customer lock timeout retries
    with a blocking sleep (Celery's ``self.retry()`` has no equivalent without a
    broker) using the same attempt budget and countdown as the worker path, then
    reports the message stalled exactly like the worker path once exhausted.
    """
    for attempt in range(MAX_LOCK_RETRIES + 1):
        try:
            _process_once(message_pk, wa_id, text, reply_id, reply_title, profile_name)
            return
        except CustomerLockTimeout:
            if attempt < MAX_LOCK_RETRIES:
                services.release_claim(message_pk)
                logger.warning(
                    "process_inbound_inline: per-customer lock timed out to=%s; retrying (%d/%d)",
                    mask_phone(wa_id), attempt + 1, MAX_LOCK_RETRIES,
                )  # fmt: skip
                time.sleep(LOCK_RETRY_COUNTDOWN_SECONDS)
            else:
                services.report_lock_exhausted(message_pk, wa_id)
                return


@shared_task(name="whatsapp.report_stalled", ignore_result=True)
def report_stalled() -> int:
    """Run every few minutes (Celery beat or ``manage.py report_stalled_messages``)."""
    return services.report_stalled_messages()

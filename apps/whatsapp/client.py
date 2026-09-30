"""WhatsApp Cloud API client (send messages), standard library only.

The transport (``apps.iva_common.http_transport``) applies explicit connect/read
timeouts. 429, 5xx and transport failures are retried with exponential backoff,
honouring ``Retry-After``. Other 4xx responses are not retried. The access token is
sent only in the Authorization header and never appears in errors or logs; nor does
the recipient number (masked) or any message text.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.iva_common.http_transport import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    HttpError,
    HttpTransport,
    StdlibTransport,
)
from apps.iva_common.logging import mask_phone

logger = logging.getLogger(__name__)

DEFAULT_GRAPH_BASE_URL = "https://graph.facebook.com"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
_API_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,3}$")
_PHONE_NUMBER_ID_RE = re.compile(r"^\d{5,30}$")


class WhatsAppSendError(Exception):
    """A message could not be sent. ``kind``: http, timeout, connection or invalid_response."""

    def __init__(
        self,
        kind: str,
        *,
        status: int | None = None,
        code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(f"WhatsApp send failed: kind={kind} status={status} code={code}")
        self.kind = kind
        self.status = status
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SendResult:
    wa_message_id: str
    attempts: int


def _error_code(body: bytes) -> int | None:
    try:
        code = json.loads(body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


class WhatsAppClient:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        base_url: str = DEFAULT_GRAPH_BASE_URL,
        transport: HttpTransport | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not access_token:
            raise ImproperlyConfigured("WHATSAPP_ACCESS_TOKEN is not set.")
        if not phone_number_id or not _PHONE_NUMBER_ID_RE.match(phone_number_id):
            raise ImproperlyConfigured("WHATSAPP_PHONE_NUMBER_ID is missing or invalid.")
        if not api_version or not _API_VERSION_RE.match(api_version):
            raise ImproperlyConfigured("WHATSAPP_API_VERSION must look like v21.0.")
        if not base_url.startswith("https://"):
            raise ImproperlyConfigured("WHATSAPP_GRAPH_BASE_URL must use https.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self._token = access_token
        self.url = f"{base_url.rstrip('/')}/{api_version}/{phone_number_id}/messages"
        self._transport = transport or StdlibTransport()
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sleep = sleep

    def __repr__(self) -> str:  # never show the token
        return f"WhatsAppClient(url={self.url!r})"

    def send(self, to: str, message: dict) -> SendResult:
        """Send one rendered message (``renderers.render`` output) to ``to``."""
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
        payload.update(message)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        for attempt in range(1, self._max_attempts + 1):
            retry_after = None
            try:
                response = self._transport.request(
                    "POST",
                    self.url,
                    headers=headers,
                    body=body,
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._read_timeout,
                )
            except HttpError as exc:
                error = WhatsAppSendError(exc.kind, retryable=True)
            else:
                if 200 <= response.status < 300:
                    return SendResult(self._message_id(response.body), attempt)
                retryable = response.status == 429 or response.status >= 500
                error = WhatsAppSendError(
                    "http",
                    status=response.status,
                    code=_error_code(response.body),
                    retryable=retryable,
                )
                retry_after = _retry_after(response.header("Retry-After"))
            if not error.retryable or attempt == self._max_attempts:
                logger.error(
                    "whatsapp send failed: to=%s kind=%s status=%s code=%s attempts=%d",
                    mask_phone(to), error.kind, error.status, error.code, attempt,
                )  # fmt: skip
                raise error
            delay = retry_after if retry_after is not None else self._backoff * 2 ** (attempt - 1)
            delay = min(delay, self._max_backoff)
            logger.warning(
                "whatsapp send retry: to=%s kind=%s status=%s attempt=%d delay=%.2f",
                mask_phone(to), error.kind, error.status, attempt, delay,
            )  # fmt: skip
            self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _message_id(body: bytes) -> str:
        try:
            message_id = json.loads(body)["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise WhatsAppSendError("invalid_response") from None
        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppSendError("invalid_response")
        return message_id


def client_from_settings(transport: HttpTransport | None = None) -> WhatsAppClient:
    return WhatsAppClient(
        access_token=getattr(settings, "WHATSAPP_ACCESS_TOKEN", ""),
        phone_number_id=getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", ""),
        api_version=getattr(settings, "WHATSAPP_API_VERSION", ""),
        base_url=getattr(settings, "WHATSAPP_GRAPH_BASE_URL", "") or DEFAULT_GRAPH_BASE_URL,
        transport=transport,
    )

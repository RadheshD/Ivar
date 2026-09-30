"""Small HTTP helper on the Python standard library, with explicit timeouts.

``urllib.request`` has a single timeout, so this uses ``http.client`` (which urllib is
built on) to apply a separate connect timeout and read timeout. HTTPS only by default,
certificate verification always on, bounded response size. Request headers and bodies
are never logged: they may hold access tokens and customer data.
"""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lowered), None)


class HttpError(Exception):
    """The request did not produce an HTTP response. ``kind``: "timeout" or "connection"."""

    def __init__(self, kind: str):
        super().__init__(f"HTTP request failed: {kind}")
        self.kind = kind


@runtime_checkable
class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> HttpResponse: ...


class StdlibTransport:
    """``HttpTransport`` over ``http.client``. ``allow_http`` is for local tests only."""

    def __init__(self, *, allow_http: bool = False, max_response_bytes: int = MAX_RESPONSE_BYTES):
        self._allow_http = allow_http
        self._max_bytes = max_response_bytes
        self._ssl = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> HttpResponse:
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("Timeouts must be greater than 0.")
        parts = urlsplit(url)
        if not parts.hostname:
            raise ValueError("URL has no host.")
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=connect_timeout, context=self._ssl
            )
        elif parts.scheme == "http" and self._allow_http:
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=connect_timeout)
        else:
            raise ValueError("Only https URLs are allowed.")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            try:
                conn.connect()
                conn.sock.settimeout(read_timeout)
                conn.request(method, path, body=body, headers=dict(headers or {}))
                response = conn.getresponse()
                data = response.read(self._max_bytes + 1)
            except TimeoutError:
                raise HttpError("timeout") from None
            except (OSError, http.client.HTTPException):
                raise HttpError("connection") from None
            if len(data) > self._max_bytes:
                raise HttpError("connection")  # refuse unbounded responses
            return HttpResponse(response.status, data, dict(response.getheaders()))
        finally:
            conn.close()

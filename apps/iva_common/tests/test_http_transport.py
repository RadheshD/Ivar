"""StdlibTransport against a throwaway local HTTP server on 127.0.0.1 (never the internet)."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apps.iva_common.http_transport import HttpError, HttpResponse, HttpTransport, StdlibTransport


class Handler(BaseHTTPRequestHandler):
    received: list = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        Handler.received.append((self.path, dict(self.headers), body))
        if self.path == "/slow":
            time.sleep(1.0)
        payload = b"x" * 2000 if self.path == "/big" else json.dumps({"ok": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "2")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_implements_the_protocol():
    assert isinstance(StdlibTransport(), HttpTransport)


def test_request_and_response(server):
    transport = StdlibTransport(allow_http=True)
    response = transport.request(
        "POST",
        f"{server}/ok?x=1",
        headers={"Content-Type": "application/json", "X-Test": "yes"},
        body=b'{"a": 1}',
        connect_timeout=2,
        read_timeout=2,
    )
    assert response.status == 201
    assert json.loads(response.body) == {"ok": True}
    assert response.header("retry-after") == "2"
    path, headers, body = Handler.received[-1]
    assert path == "/ok?x=1"
    assert headers["X-Test"] == "yes"
    assert body == b'{"a": 1}'


def test_read_timeout(server):
    transport = StdlibTransport(allow_http=True)
    started = time.monotonic()
    with pytest.raises(HttpError) as info:
        transport.request("POST", f"{server}/slow", body=b"", connect_timeout=2, read_timeout=0.2)
    assert info.value.kind == "timeout"
    assert time.monotonic() - started < 0.9


def test_connection_refused():
    with socket.socket() as sock:  # find a port with nothing listening
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(HttpError) as info:
        StdlibTransport(allow_http=True).request(
            "POST", f"http://127.0.0.1:{port}/", connect_timeout=0.5, read_timeout=0.5
        )
    assert info.value.kind in ("connection", "timeout")


def test_oversized_response_is_refused(server):
    transport = StdlibTransport(allow_http=True, max_response_bytes=1000)
    with pytest.raises(HttpError):
        transport.request("POST", f"{server}/big", body=b"")


@pytest.mark.parametrize("url", ["http://example.invalid/", "ftp://example.invalid/", "https:///x"])
def test_only_https_by_default(url):
    with pytest.raises(ValueError):
        StdlibTransport().request("POST", url)


def test_timeouts_must_be_positive():
    with pytest.raises(ValueError):
        StdlibTransport().request("POST", "https://example.invalid/", connect_timeout=0)


def test_error_text_contains_no_request_details():
    error = HttpError("timeout")
    assert str(error) == "HTTP request failed: timeout"


def test_header_lookup_is_case_insensitive():
    response = HttpResponse(200, b"", {"Retry-After": "3"})
    assert response.header("RETRY-AFTER") == "3"
    assert response.header("missing") is None

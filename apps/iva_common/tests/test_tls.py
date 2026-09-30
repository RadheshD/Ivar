"""The HTTPS transport always verifies certificates and hostnames.

A local HTTPS server on 127.0.0.1 presents a self-signed certificate (Python's own test
certificate). A verifying client must refuse it, even when the usual global switches
for disabling verification are flipped.
"""

import inspect
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.iva_common import http_transport
from apps.iva_common.http_transport import HttpError, StdlibTransport

CERT = Path(ssl.__file__).parent / "test" / "certdata" / "keycert.pem"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture(scope="module")
def https_server():
    if not CERT.exists():
        pytest.skip("Python's test certificate is not installed")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_context_verifies_certificates_and_hostnames():
    context = StdlibTransport()._ssl
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_verification_has_no_off_switch():
    # Only these options exist; none of them touches TLS verification.
    assert set(inspect.signature(StdlibTransport).parameters) == {
        "allow_http",
        "max_response_bytes",
    }
    source = inspect.getsource(http_transport)
    for switch in ("CERT_NONE", "check_hostname = False", "_create_unverified_context"):
        assert switch not in source


def test_self_signed_certificate_is_refused(https_server):
    with pytest.raises(HttpError) as info:
        StdlibTransport().request("POST", f"{https_server}/", body=b"{}")
    assert info.value.kind == "connection"


def test_global_opt_outs_do_not_disable_verification(https_server, monkeypatch):
    monkeypatch.setenv("PYTHONHTTPSVERIFY", "0")
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    with pytest.raises(HttpError):
        StdlibTransport().request("POST", f"{https_server}/", body=b"{}")
    assert os.environ["PYTHONHTTPSVERIFY"] == "0"  # the opt-out really was set


def test_the_certificate_would_otherwise_be_served(https_server):
    # Control: the server works for a client that trusts the test certificate, so the
    # refusal above is certificate verification, not a broken server.
    import http.client
    from urllib.parse import urlsplit

    context = ssl.create_default_context(cafile=str(CERT))
    context.check_hostname = False  # the test certificate is issued to "localhost"
    parts = urlsplit(https_server)
    conn = http.client.HTTPSConnection(parts.hostname, parts.port, context=context, timeout=3)
    conn.request("POST", "/", body=b"{}")
    assert conn.getresponse().status == 200
    conn.close()

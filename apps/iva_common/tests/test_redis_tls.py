"""Every Redis connection (cache, conversation sessions, Celery broker) is built with
``ssl_cert_reqs=ssl.CERT_REQUIRED``. This proves that setting is actually enforced, the
same way ``test_tls.py`` proves it for the HTTPS transport: a local TCP server wrapped
in Python's own self-signed test certificate must be refused by a verifying client, and
accepted by an unverifying one (the control), so the refusal is certificate verification
and not a broken server.
"""

import socket
import ssl
import threading
from pathlib import Path

import pytest
import redis

CERT = Path(ssl.__file__).parent / "test" / "certdata" / "keycert.pem"


def _fake_tls_redis_server(certfile: Path):
    """A bare TCP+TLS listener that speaks just enough RESP2 to answer PING."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw.bind(("127.0.0.1", 0))
    raw.listen(5)
    port = raw.getsockname()[1]

    def serve():
        try:
            conn, _ = raw.accept()
        except OSError:
            return
        try:
            tls_conn = context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            # The verifying client refused the handshake. Depending on timing it either
            # sends a clean TLS alert (SSLError) or just resets the TCP connection
            # (OSError); both mean the same thing here, so nothing more to do.
            return
        try:
            while True:
                if not tls_conn.recv(4096):
                    break
                tls_conn.sendall(b"+PONG\r\n")
        except OSError:
            pass
        finally:
            tls_conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, raw


@pytest.fixture
def tls_server():
    if not CERT.exists():
        pytest.skip("Python's test certificate is not installed")
    port, raw = _fake_tls_redis_server(CERT)
    yield port
    raw.close()


def _client(port, *, ssl_cert_reqs):
    return redis.Redis(
        host="127.0.0.1",
        port=port,
        ssl=True,
        ssl_cert_reqs=ssl_cert_reqs,
        socket_connect_timeout=3,
        socket_timeout=3,
        protocol=2,
    )


def test_verifying_client_refuses_the_self_signed_certificate(tls_server):
    client = _client(tls_server, ssl_cert_reqs=ssl.CERT_REQUIRED)
    with pytest.raises(redis.exceptions.ConnectionError):
        client.ping()


def test_the_certificate_would_otherwise_be_accepted(tls_server):
    # Control: an unverifying client accepts the same self-signed certificate and
    # completes the PING, proving the refusal above is certificate verification and
    # not a broken server or an unrelated connection failure.
    client = _client(tls_server, ssl_cert_reqs=ssl.CERT_NONE)
    assert client.ping() is True

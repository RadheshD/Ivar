"""Webhook authentication: Meta's HMAC-SHA256 signature and the subscription verify token.

Both comparisons are constant time. An empty configured secret or token never matches.
"""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def verify_signature(app_secret: str | None, raw_body: bytes, header: str | None) -> bool:
    """True when ``header`` is ``sha256=<hex HMAC of the raw body with the app secret>``."""
    if not app_secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    given = header[len(SIGNATURE_PREFIX) :].strip().lower()
    return hmac.compare_digest(expected.encode("ascii"), given.encode("utf-8"))


def verify_token(expected: str | None, given: str | None) -> bool:
    if not expected or given is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))

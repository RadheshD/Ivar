"""The welcome banner as a WhatsApp media ID: validate the local file, upload it once, cache the ID.

Used only by the delivery layer. The conversation engine never sees this: it carries the
abstract ``WELCOME_BANNER_REF`` on the first-time language picker, and ``delivery`` resolves it
here. Nothing in this module may ever raise to the caller: a missing or unsuitable file, a failed
or slow upload, or an unreachable cache all end in "no media ID", and the picker goes out
text-only.

Media IDs from the upload endpoint stay valid for 30 days (Meta's Cloud API media reference), so
the cached ID lives 20 days (``IVA_WELCOME_BANNER_MEDIA_TTL_SECONDS`` overrides it) and is
re-uploaded after that. The cache key holds the file's content hash and the phone number ID, so
a changed file or another number never reuses an ID. Neither the token, the file bytes nor the
media ID is logged.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from .client import WhatsAppClient

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024  # WhatsApp's limit for an image
DEFAULT_TTL_SECONDS = 20 * 24 * 3600  # well under the 30 days a media ID stays valid
KEY_PREFIX = "iva:media:welcome:"
UPLOAD_CONNECT_TIMEOUT = 3.0
UPLOAD_READ_TIMEOUT = 5.0
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


@dataclass(frozen=True)
class Banner:
    data: bytes
    mime_type: str
    filename: str
    sha256: str


_lock = threading.Lock()
# (path, mtime_ns, size) -> Banner, or None when the file is unusable. A bad file is therefore
# judged (and warned about) once per file state, not once per customer.
_memo: dict[tuple, Banner | None] = {}


def _path() -> Path | None:
    raw = getattr(settings, "IVA_WELCOME_BANNER_FILE", "") or ""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(getattr(settings, "BASE_DIR", ".")) / path
    return path


def load_banner() -> Banner | None:
    """The validated local banner, or ``None`` (not configured, missing, wrong type, too big)."""
    path = _path()
    if path is None:
        return None
    try:
        stat = path.stat()
        signature = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (str(path), None, None)
    with _lock:
        if signature in _memo:
            return _memo[signature]
        banner, reason = _read(path, signature)
        if banner is None:
            logger.warning("welcome banner file not used: %s", reason)
        _memo.clear()  # keep one entry: the file's current state
        _memo[signature] = banner
        return banner


def _read(path: Path, signature: tuple) -> tuple[Banner | None, str]:
    if signature[1] is None:
        return None, "file not found or unreadable"
    if signature[2] > MAX_BYTES:
        return None, "file is over 5 MB"
    try:
        data = path.read_bytes()
    except OSError:
        return None, "file not found or unreadable"
    if data.startswith(_PNG):
        mime = "image/png"
    elif data.startswith(_JPEG):
        mime = "image/jpeg"
    else:
        return None, "file is not a PNG or JPEG"
    return Banner(data, mime, path.name, hashlib.sha256(data).hexdigest()), ""


def _ttl() -> int:
    return int(getattr(settings, "IVA_WELCOME_BANNER_MEDIA_TTL_SECONDS", DEFAULT_TTL_SECONDS))


def _key(client: WhatsAppClient, banner: Banner) -> str:
    return f"{KEY_PREFIX}{client.phone_number_id}:{banner.sha256}"


def media_id_for(client: WhatsAppClient, *, force_upload: bool = False) -> tuple[str | None, bool]:
    """``(media_id, came_from_cache)``. ``(None, False)`` when there is no usable ID.

    Uses the cached ID unless ``force_upload``; otherwise uploads once (short timeout) and
    caches the result. Never raises.
    """
    try:
        banner = load_banner()
        if banner is None:
            return None, False
        key = _key(client, banner)
        if not force_upload:
            try:
                cached = cache.get(key)
            except Exception:  # an unreachable cache is a miss, not an error
                cached = None
            if isinstance(cached, str) and cached:
                return cached, True
        try:
            media_id = client.upload_media(
                banner.data,
                banner.mime_type,
                banner.filename,
                connect_timeout=UPLOAD_CONNECT_TIMEOUT,
                read_timeout=UPLOAD_READ_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "welcome banner upload failed: %s", getattr(exc, "kind", type(exc).__name__)
            )
            return None, False
        try:
            cache.set(key, media_id, _ttl())
        except Exception:
            logger.warning("welcome banner media ID could not be cached")
        return media_id, False
    except Exception as exc:  # belt and braces: the picker must always go out
        logger.warning("welcome banner unavailable: %s", type(exc).__name__)
        return None, False


def forget(client: WhatsAppClient) -> None:
    """Drop the cached ID (WhatsApp rejected it and a fresh upload did not help)."""
    try:
        banner = load_banner()
        if banner is not None:
            cache.delete(_key(client, banner))
    except Exception as exc:  # never raises: a stale ID just expires on its own
        logger.warning("welcome banner cache entry could not be dropped: %s", type(exc).__name__)

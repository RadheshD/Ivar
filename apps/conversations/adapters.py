"""Load the configured Backend Port adapter (``settings.IVA_BACKEND_ADAPTER``)."""

import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .backend_port import BackendPort, port_operations

DEFAULT_ADAPTER = "apps.conversations.fake_backend.FakeBackend"

_lock = threading.Lock()
_instance: BackendPort | None = None
_instance_path: str | None = None


class BackendConfigurationError(ImproperlyConfigured):
    """``IVA_BACKEND_ADAPTER`` is invalid or names a class that does not implement the port."""


def load_adapter_class(path: str) -> type:
    """Import ``path`` and check that it is a class implementing every port operation."""
    if not path or not isinstance(path, str):
        raise BackendConfigurationError("IVA_BACKEND_ADAPTER is not set.")
    try:
        cls = import_string(path)
    except ImportError as exc:
        raise BackendConfigurationError(
            f"IVA_BACKEND_ADAPTER={path!r} could not be imported."
        ) from exc
    if not isinstance(cls, type):
        raise BackendConfigurationError(f"IVA_BACKEND_ADAPTER={path!r} is not a class.")
    missing = [name for name in port_operations() if not callable(getattr(cls, name, None))]
    if missing:
        raise BackendConfigurationError(
            f"IVA_BACKEND_ADAPTER={path!r} does not implement: {', '.join(missing)}."
        )
    return cls


def get_backend() -> BackendPort:
    """Return the shared adapter instance, creating it on first use."""
    global _instance, _instance_path
    path = getattr(settings, "IVA_BACKEND_ADAPTER", None) or DEFAULT_ADAPTER
    with _lock:
        if _instance is None or _instance_path != path:
            _instance = load_adapter_class(path)()
            _instance_path = path
        return _instance


def reset_backend() -> None:
    """Drop the shared instance (tests, or after changing settings)."""
    global _instance, _instance_path
    with _lock:
        _instance = None
        _instance_path = None

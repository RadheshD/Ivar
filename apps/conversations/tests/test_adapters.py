import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.conversations.adapters import BackendConfigurationError, get_backend, load_adapter_class
from apps.conversations.backend_port import BackendPort
from apps.conversations.fake_backend import FakeBackend

HERE = "apps.conversations.tests.test_adapters"


class IncompleteBackend:
    def get_or_create_customer(self, whatsapp_id):
        raise NotImplementedError

    def get_cart(self, customer_id):
        raise NotImplementedError


class SubclassedFake(FakeBackend):
    pass


NOT_A_CLASS = object()


def test_loads_configured_fake_backend():
    backend = get_backend()
    assert isinstance(backend, FakeBackend)
    assert isinstance(backend, BackendPort)


def test_returns_shared_instance():
    assert get_backend() is get_backend()


def test_switches_when_setting_changes():
    default = get_backend()
    with override_settings(IVA_BACKEND_ADAPTER=f"{HERE}.SubclassedFake"):
        other = get_backend()
        assert type(other) is SubclassedFake
    assert other is not default


@pytest.mark.parametrize(
    "path",
    [
        "apps.conversations.nope.Backend",
        "apps.conversations.fake_backend.Missing",
        "not-a-dotted-path",
    ],
)
def test_invalid_path_fails_clearly(path):
    with override_settings(IVA_BACKEND_ADAPTER=path):
        with pytest.raises(BackendConfigurationError, match="could not be imported"):
            get_backend()


def test_not_a_class_fails_clearly():
    with override_settings(IVA_BACKEND_ADAPTER=f"{HERE}.NOT_A_CLASS"):
        with pytest.raises(BackendConfigurationError, match="is not a class"):
            get_backend()


def test_incomplete_class_lists_missing_operations():
    with pytest.raises(BackendConfigurationError) as exc:
        load_adapter_class(f"{HERE}.IncompleteBackend")
    message = str(exc.value)
    assert "does not implement" in message
    assert "checkout" in message and "list_products" in message
    assert "get_cart," not in message and "get_or_create_customer" not in message


@pytest.mark.parametrize("missing", ["get_order_detail", "get_customer"])
def test_adapter_without_a_new_order_or_customer_method_is_rejected(missing):
    cls = type("PartialFake", (FakeBackend,), {missing: None})
    globals()["PartialFake"] = cls  # import_string needs a module attribute
    try:
        with pytest.raises(BackendConfigurationError, match=missing):
            load_adapter_class(f"{HERE}.PartialFake")
    finally:
        del globals()["PartialFake"]


def test_empty_path_fails_clearly():
    with pytest.raises(BackendConfigurationError, match="not set"):
        load_adapter_class("")


def test_configuration_error_is_improperly_configured():
    assert issubclass(BackendConfigurationError, ImproperlyConfigured)

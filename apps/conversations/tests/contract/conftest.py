import os

import pytest
from django.utils.module_loading import import_string

from .harness import BUILTIN_HARNESSES


def _harness_paths() -> dict[str, str]:
    paths = dict(BUILTIN_HARNESSES)
    extra = os.environ.get("IVA_CONTRACT_HARNESS", "").strip()
    if extra:
        paths[extra.rsplit(".", 1)[-1]] = extra
    return paths


@pytest.fixture(params=sorted(_harness_paths().items()), ids=lambda item: item[0])
def harness(request):
    h = import_string(request.param[1])()
    yield h
    h.close()


@pytest.fixture
def backend(harness):
    return harness.backend


@pytest.fixture
def customer(backend, harness):
    return backend.get_or_create_customer(harness.new_whatsapp_id())


@pytest.fixture
def other_customer(backend, harness):
    return backend.get_or_create_customer(harness.new_whatsapp_id())

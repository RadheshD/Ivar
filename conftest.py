import pytest

from apps.conversations.adapters import reset_backend


@pytest.fixture(autouse=True)
def _fresh_backend():
    """Every test starts with a new shared backend instance."""
    reset_backend()
    yield
    reset_backend()

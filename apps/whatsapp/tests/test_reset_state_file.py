"""``reset_dev_data`` and the Fake Backend dev state file.

Every other effect of the command (Postgres, Redis, Celery) is stubbed out here, so these
tests can never delete real data; the state file is a temporary one.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.conversations.fake_backend import FakeBackend, state_file_counts
from apps.whatsapp import dev_reset
from apps.whatsapp.management.commands import reset_dev_data as command

EMPTY = {"customers": 0, "cart lines": 0, "addresses": 0, "orders": 0}


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "fake_backend.json"
    backend = FakeBackend(state_file=path)
    customer = backend.get_or_create_customer("919876500001")
    backend.add_item(customer.id, "seed-mix--std", 1)
    return path


@pytest.fixture
def stubbed(monkeypatch):
    calls = []
    monkeypatch.setattr(command, "count_postgres", lambda: {"conversations_conversation": 0})
    monkeypatch.setattr(command, "count_redis", lambda: {"sessions": 0})
    monkeypatch.setattr(command, "count_celery_queue", lambda: 0)
    monkeypatch.setattr(command, "delete_postgres", lambda: calls.append("pg") or {"x": 0})
    monkeypatch.setattr(command, "delete_redis", lambda: calls.append("redis") or {"x": 0})
    monkeypatch.setattr(command, "purge_celery_queue", lambda: calls.append("celery") or 0)
    return calls


def run(*args):
    out = StringIO()
    with override_settings(IVA_DEV_RESET_ALLOWED=True):
        call_command("reset_dev_data", *args, stdout=out)
    return out.getvalue()


def test_dry_run_reports_counts_and_keeps_the_file(state_path, stubbed):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(state_path)):
        text = run()
    assert "Fake Backend state file:" in text and "customers: 1" in text
    assert state_file_counts(state_path)["customers"] == 1
    assert stubbed == []


def test_confirm_clears_the_file(state_path, stubbed):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(state_path)):
        text = run("--confirm")
    assert state_file_counts(state_path) == EMPTY
    assert state_path.exists()  # emptied, not removed
    assert "customers: 1" in text  # reports what was cleared
    assert stubbed == ["pg", "redis", "celery"]


def test_without_a_state_file_the_command_is_unchanged(stubbed, tmp_path):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=""):
        text = run("--confirm")
    assert "Fake Backend state file" not in text
    assert list(tmp_path.iterdir()) == []


def test_refuses_outside_dev_and_leaves_the_file_alone(state_path, stubbed):
    from django.core.management.base import CommandError

    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(state_path)):
        with override_settings(IVA_DEV_RESET_ALLOWED=False), pytest.raises(CommandError):
            call_command("reset_dev_data", "--confirm", stdout=StringIO())
    assert state_file_counts(state_path)["customers"] == 1


def test_helpers_return_empty_when_nothing_is_configured():
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=""):
        assert dev_reset.count_fake_backend_state() == {}
        assert dev_reset.clear_fake_backend_state() == {}

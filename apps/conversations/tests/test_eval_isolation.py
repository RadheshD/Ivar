"""eval_intent_llm must never touch the dev Fake Backend state file, and stays quiet.

The "dev state file" here is a temporary stand-in pointed at by
``settings.IVA_FAKE_BACKEND_STATE_FILE``; nothing touches the developer's real one.
"""

from __future__ import annotations

import io
import logging
import os

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai.groq import GroqClassifier
from apps.conversations import intent_eval
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.intent_eval import EvalIsolationError, Harness
from apps.conversations.management.commands import eval_intent_llm

from .test_eval_harness import MockedGroq

WA = "919876500777"


@pytest.fixture
def dev(settings):
    settings.IVA_DEV_RESET_ALLOWED = True
    settings.GROQ_API_KEY = "gsk_unit_test_key_not_real"
    return settings


@pytest.fixture
def mocked(monkeypatch):
    def install():
        transport = MockedGroq()
        monkeypatch.setattr(
            eval_intent_llm,
            "build_classifier",
            lambda key, model: GroqClassifier(key, model, transport=transport),
        )
        return transport

    return install


@pytest.fixture
def dev_state(tmp_path, settings):
    """A populated state file that the default FakeBackend() would read and write."""
    path = tmp_path / "fake_backend.json"
    seed = FakeBackend(state_file=path)
    customer = seed.get_or_create_customer(WA)
    seed.add_item(customer.id, "seed-mix--std", 2)
    settings.IVA_FAKE_BACKEND_STATE_FILE = str(path)
    return path


def fingerprint(path):
    st = os.stat(path)
    return path.read_bytes(), st.st_mtime_ns, st.st_size


def run(tmp_path, name, **kwargs):
    kwargs.setdefault("sleep", 0)
    call_command("eval_intent_llm", output=str(tmp_path / name), stdout=io.StringIO(), **kwargs)


def test_positive_control_a_default_backend_would_write_the_dev_file(dev_state):
    before = fingerprint(dev_state)
    backend = FakeBackend()  # reads settings: exactly what the eval used to do
    backend.get_or_create_customer("919876500888")
    assert fingerprint(dev_state)[0] != before[0]  # proves the file is really wired up


def test_the_harness_never_reads_or_writes_the_configured_state_file(dev_state):
    harness = Harness()
    assert harness.backend._state_file is None
    customers = harness.backend.get_or_create_customer(WA)
    assert customers.id == "cust_1"  # a fresh in-memory world, not the saved customer


def test_dry_run_of_both_sets_and_a_mocked_live_run_leave_the_file_untouched(
    dev,
    mocked,
    dev_state,
    tmp_path,
):
    before = fingerprint(dev_state)
    for set_name in ("intents", "recommend"):
        run(tmp_path, f"dry_{set_name}.json", golden_set=set_name, dry_run=True)
    run(tmp_path, "dry_verbose.json", dry_run=True, verbose=True, limit=5)
    mocked()
    run(tmp_path, "live_intents.json", ids="g001,g002,g003")
    run(tmp_path, "live_recommend.json", golden_set="recommend", ids="r001,r002,r003")
    assert fingerprint(dev_state) == before  # contents, mtime and size all unchanged


def test_no_stray_state_file_is_created_elsewhere(dev, dev_state, tmp_path):
    run(tmp_path, "r.json", dry_run=True, limit=5)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fake_backend.json", "r.json"]


def test_a_backend_that_would_persist_makes_the_eval_fail_loudly(dev_state):
    with pytest.raises(EvalIsolationError, match="persist"):
        Harness(backend=FakeBackend(state_file=dev_state))
    with pytest.raises(EvalIsolationError):
        Harness(backend=object())


def test_the_command_refuses_to_run_if_isolation_is_impossible(
    dev,
    dev_state,
    tmp_path,
    monkeypatch,
):
    before = fingerprint(dev_state)
    monkeypatch.setattr(intent_eval, "isolated_backend", lambda: FakeBackend(state_file=dev_state))
    with pytest.raises(CommandError, match="persist"):
        run(tmp_path, "r.json", dry_run=True)
    assert fingerprint(dev_state) == before


# ---------------------------------------------------------------- quiet / verbose


def test_quiet_is_the_default_and_the_summary_still_prints(dev, tmp_path, caplog):
    out = io.StringIO()
    with caplog.at_level(logging.INFO):
        call_command(
            "eval_intent_llm", dry_run=True, limit=6, sleep=0,
            output=str(tmp_path / "r.json"), stdout=out,
        )  # fmt: skip
    assert not [r for r in caplog.records if "backend=fake" in r.getMessage()]
    assert "SAFETY: 0 unsafe" in out.getvalue()
    assert logging.root.manager.disable == 0  # logging is restored afterwards


def test_verbose_restores_the_per_call_logs(dev, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        run(tmp_path, "r.json", dry_run=True, limit=3, verbose=True)
    assert [r for r in caplog.records if "backend=fake" in r.getMessage()]
    assert logging.root.manager.disable == 0


def test_warnings_still_get_through_when_quiet():
    with intent_eval.quiet_logs(True):
        assert not logging.getLogger("x").isEnabledFor(logging.INFO)
        assert logging.getLogger("x").isEnabledFor(logging.WARNING)
    assert logging.getLogger("x").isEnabledFor(logging.WARNING)


# ---------------------------------------------------------------- the suite itself


def test_the_test_settings_never_point_at_a_dev_state_file(settings):
    assert settings.IVA_FAKE_BACKEND_STATE_FILE == ""


# ---------------------------------------------------------------- other dev commands


def test_export_meta_catalog_only_reads_the_dev_state_file(dev_state, tmp_path):
    from apps.conversations.adapters import reset_backend

    before = fingerprint(dev_state)
    reset_backend()  # make get_backend() pick up the state file configured above
    try:
        with pytest.raises(CommandError):  # no image base URL in tests: it validates and stops
            call_command(
                "export_meta_catalog", output=str(tmp_path / "c.csv"), stdout=io.StringIO()
            )
    finally:
        reset_backend()
    assert fingerprint(dev_state) == before

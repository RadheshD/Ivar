"""The test suite never uses the developer's saved Fake Backend state file.

The test settings module blanks ``IVA_FAKE_BACKEND_STATE_FILE`` whatever the environment says,
and a guard in ``conftest.py`` fails loudly if a path outside pytest's own temp directory is
ever resolved. Nothing here touches a real state file: the "dev" paths are stand-ins.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.test import override_settings

from apps.conversations.fake_backend import FakeBackend, _state_file_from_settings

ROOT = Path(__file__).resolve().parents[3]


def test_the_test_settings_blank_the_dev_state_file():
    assert settings.IVA_FAKE_BACKEND_STATE_FILE == ""
    assert _state_file_from_settings() == ""
    assert FakeBackend()._state_file is None  # in memory only


def test_the_test_settings_blank_it_even_when_the_environment_sets_one(tmp_path):
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": settings.SETTINGS_MODULE,
        "IVA_FAKE_BACKEND_STATE_FILE": str(tmp_path / "stand-in-dev-state.json"),
    }
    code = (
        "import django; django.setup(); from django.conf import settings; "
        "print(repr(settings.IVA_FAKE_BACKEND_STATE_FILE))"
    )
    out = subprocess.run(  # noqa: S603  (our own interpreter and a fixed snippet)
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().splitlines()[-1] == "''"


def test_a_state_file_outside_the_pytest_temp_dir_fails_loudly():
    stand_in = ROOT / "stand-in-dev-state.json"  # never created: the guard raises first
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(stand_in)):
        with pytest.raises(AssertionError, match="never use the dev Fake Backend state file"):
            FakeBackend()
    assert not stand_in.exists()


def test_a_state_file_inside_the_pytest_temp_dir_is_allowed(tmp_path):
    with override_settings(IVA_FAKE_BACKEND_STATE_FILE=str(tmp_path / "state.json")):
        assert FakeBackend()._state_file == tmp_path / "state.json"

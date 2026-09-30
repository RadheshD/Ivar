"""There is no admin panel of any kind."""

from pathlib import Path

from django.conf import settings
from django.urls import get_resolver

REPO_ROOT = Path(settings.BASE_DIR)
SKIP_DIRS = {".venv", ".local-services", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


def _repo_files():
    for path in REPO_ROOT.rglob("*"):
        if not any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            yield path


def test_admin_app_not_installed():
    assert "django.contrib.admin" not in settings.INSTALLED_APPS
    assert not any("admin" in app for app in settings.INSTALLED_APPS)


def test_admin_url_returns_404(client):
    for url in ("/admin/", "/admin", "/admin/login/"):
        response = client.get(url)
        assert response.status_code == 404
        assert response.json() == {"error": "not_found"}


def test_no_admin_route_registered():
    patterns = [str(p.pattern) for p in get_resolver().url_patterns]
    assert not any("admin" in p for p in patterns)


def test_no_admin_py_anywhere():
    offenders = [str(p) for p in _repo_files() if p.name == "admin.py"]
    assert offenders == []

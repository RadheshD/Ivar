"""Static check: the delivered apps import nothing from the host and respect layering.

Allowed direction: whatsapp -> conversations -> ai -> iva_common. A package may use
any package downstream of it; nothing may import ``config`` or ``host``. Third-party
imports are limited to the dependencies this project declares.
"""

import ast
import sys
from pathlib import Path

import pytest

APPS_DIR = Path(__file__).resolve().parents[2]
DELIVERED = ("whatsapp", "conversations", "ai", "iva_common")
ALLOWED_DEPS = {
    "whatsapp": {"conversations", "ai", "iva_common"},
    "conversations": {"ai", "iva_common"},
    "ai": {"iva_common"},
    "iva_common": set(),
}
THIRD_PARTY = {"django", "celery", "redis", "pytest"}
HOST_PACKAGES = {"config", "host"}


def module_parts(path: Path, apps_dir: Path) -> list[str]:
    parts = list(path.relative_to(apps_dir.parent).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def imported_modules(path: Path, apps_dir: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = module_parts(path, apps_dir)
    package = parts if path.name == "__init__.py" else parts[:-1]
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 >= len(package):
                    found.append((node.lineno, "<relative import above apps>"))
                    continue
                base = package[: len(package) - (node.level - 1)]
                target = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                target = node.module or ""
            if target == "apps":
                found.extend((node.lineno, f"apps.{a.name}") for a in node.names)
            else:
                found.append((node.lineno, target))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Dotted-path strings (import_string, settings defaults) count as imports.
            value = node.value
            if value.split(".")[0] in HOST_PACKAGES and "." in value and " " not in value:
                found.append((node.lineno, value))
    return found


def violations_for(path: Path, own: str, apps_dir: Path = APPS_DIR) -> list[str]:
    problems = []
    for lineno, name in imported_modules(path, apps_dir):
        parts = name.split(".")
        where = f"{path.relative_to(apps_dir.parent)}:{lineno} imports {name}"
        if parts[0] in HOST_PACKAGES:
            problems.append(f"{where} (host package)")
        elif parts[0] == "apps":
            target = parts[1] if len(parts) > 1 else None
            if target not in DELIVERED:
                problems.append(f"{where} (not a delivered package)")
            elif target != own and target not in ALLOWED_DEPS[own]:
                problems.append(f"{where} (breaks {own} layering)")
        elif parts[0].startswith("<"):
            problems.append(f"{where} (escapes the apps package)")
        elif parts[0] not in sys.stdlib_module_names and parts[0] not in THIRD_PARTY:
            problems.append(f"{where} (undeclared dependency)")
    return problems


def delivered_files():
    for package in DELIVERED:
        root = APPS_DIR / package
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                yield package, path


def test_delivered_packages_present():
    assert (APPS_DIR / "conversations").is_dir()
    assert (APPS_DIR / "iva_common").is_dir()
    present = {p.name for p in APPS_DIR.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert present <= set(DELIVERED)


def test_delivered_apps_are_portable():
    files = list(delivered_files())
    assert len(files) > 5
    problems = [v for package, path in files for v in violations_for(path, package)]
    assert problems == []


@pytest.mark.parametrize(
    ("package", "source", "fragment"),
    [
        ("conversations", "from config.settings import base\n", "host package"),
        ("conversations", "import host.views\n", "host package"),
        ("ai", "from apps.whatsapp import client\n", "breaks ai layering"),
        ("ai", "from apps import conversations\n", "breaks ai layering"),
        ("ai", "from ..conversations import engine\n", "breaks ai layering"),
        ("iva_common", "from apps.ai import rules\n", "breaks iva_common layering"),
        ("conversations", "import requests\n", "undeclared dependency"),
        ("conversations", "X = 'host.views.live'\n", "host package"),
        ("conversations", "from ...config import x\n", "escapes the apps package"),
    ],
)
def test_checker_detects_violations(tmp_path, package, source, fragment):
    fake_apps = tmp_path / "apps"
    target = fake_apps / package / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    problems = violations_for(target, package, fake_apps)
    assert problems and fragment in problems[0]


def test_checker_allows_downstream_imports(tmp_path):
    fake_apps = tmp_path / "apps"
    target = fake_apps / "whatsapp" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "import json\nfrom django.conf import settings\nfrom apps.conversations import engine\n"
        "from apps.ai import pipeline\nfrom apps.iva_common import money\nfrom . import client\n"
        "from ..iva_common.logging import MaskingFilter\n",
        encoding="utf-8",
    )
    assert violations_for(target, "whatsapp", fake_apps) == []

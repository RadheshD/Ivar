"""apps.ai imports nothing from apps.conversations, apps.whatsapp, config or host, uses no
SDK/HTTP/validation library, and its output carries no business data."""

import ast
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from apps.ai.intents import Entities, IntentResult

AI_DIR = Path(__file__).resolve().parents[1]
ROOT = AI_DIR.parents[1]
FORBIDDEN = ("apps.conversations", "apps.whatsapp", "config", "host")
FORBIDDEN_LIBRARIES = (
    "requests", "httpx", "urllib3", "aiohttp", "openai", "anthropic", "google", "pydantic",
    "jsonschema", "rapidfuzz", "thefuzz", "urllib.request", "http.client",
)  # fmt: skip


def _absolute_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.level == 1, f"{path.name}: relative import leaves apps.ai"
                continue
            module = node.module or ""
            names.append(module)
            if module == "apps":
                names.extend(f"apps.{alias.name}" for alias in node.names)
    return names


def _source_files():
    return sorted(p for p in AI_DIR.rglob("*.py") if "tests" not in p.parts)


def _is_under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def test_ai_imports_stay_inside_the_boundary():
    files = _source_files()
    assert len(files) >= 8
    for path in files:
        for name in _absolute_imports(path):
            assert not any(_is_under(name, f) for f in FORBIDDEN), f"{path.name} imports {name}"
            assert not any(_is_under(name, lib) for lib in FORBIDDEN_LIBRARIES), (
                f"{path.name} imports {name}"
            )
            if _is_under(name, "apps"):
                assert _is_under(name, "apps.ai") or _is_under(name, "apps.iva_common"), name


def test_importing_the_pipeline_loads_no_forbidden_module():
    code = (
        "import sys; import apps.ai.pipeline; "
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]; "
        "print(','.join(bad))"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""


def test_output_has_no_business_data_fields():
    entity_fields = {f.name for f in fields(Entities)}
    assert entity_fields == {
        "product_query", "category_query", "quantity", "unit", "language_choice",
        "order_number", "choice_index", "reference_id",
        # PLAN_RECEIVED only: restated customer-supplied targets, never business data
        # looked up or computed by the channel.
        "goal", "diet", "calories", "protein_g", "picks",
    }  # fmt: skip
    all_fields = entity_fields | {f.name for f in fields(IntentResult)}
    for word in ("price", "stock", "total", "payment_status", "order_status", "product_id"):
        assert not any(word in name for name in all_fields)

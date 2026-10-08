"""Keep transport, telemetry, and application code independently importable."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_generic_runtime_has_no_domain_or_database_imports():
    forbidden = (
        "policyengine_telemetry",
        "microcosm",
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
    )
    for path in (ROOT / "packages/local-service/src").rglob("*.py"):
        assert not any(name.startswith(forbidden) for name in imports(path)), path


def test_client_has_no_service_or_database_imports():
    root = ROOT / "packages/telemetry/src/policyengine_telemetry"
    forbidden = (
        "policyengine_telemetry.service",
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
        "microcosm",
    )
    for path in root.rglob("*.py"):
        if "service" not in path.relative_to(root).parts:
            assert not any(name.startswith(forbidden) for name in imports(path)), path


def test_public_imports_have_no_runtime_side_effects():
    script = """
import sys
sys.dont_write_bytecode = True
def audit(event, args):
    if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.mkdir"}:
        raise AssertionError(event)
sys.addaudithook(audit)
import policyengine_local_service
import policyengine_telemetry
assert not any(name in sys.modules for name in ("sqlalchemy", "alembic", "huggingface_hub"))
"""
    subprocess.run([sys.executable, "-c", script], check=True)

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
        "microcosm_emitter.telemetry",
        "microcosm",
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
    )
    paths = list((ROOT / "src/microcosm_emitter/host").rglob("*.py"))
    assert paths
    for path in paths:
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for name in imports(path)
            for prefix in forbidden
        ), path


def test_client_has_no_service_or_database_imports():
    root = ROOT / "src/microcosm_emitter/telemetry"
    forbidden = (
        "microcosm_emitter.telemetry.service",
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
        "microcosm",
    )
    paths = list(root.rglob("*.py"))
    assert paths
    for path in paths:
        if "service" not in path.relative_to(root).parts:
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in imports(path)
                for prefix in forbidden
            ), path


def test_public_imports_have_no_runtime_side_effects():
    script = """
import sys
sys.dont_write_bytecode = True
def audit(event, args):
    if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.mkdir"}:
        raise AssertionError(event)
sys.addaudithook(audit)
import microcosm_emitter
import microcosm_emitter.host
import microcosm_emitter.telemetry
import microcosm_emitter.telemetry.client
assert not any(name in sys.modules for name in (
    "sqlalchemy", "alembic", "huggingface_hub",
    "microcosm_emitter.telemetry.service",
    "microcosm_emitter.telemetry.service.lifecycle",
    "microcosm_emitter.telemetry.service.sanitization",
))
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_database_code_has_no_raw_sql_or_manual_schema_creation():
    paths = list((ROOT / "src/microcosm_emitter/telemetry").rglob("*.py"))
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                assert name not in {
                    "create_all",
                    "exec_driver_sql",
                    "executescript",
                    "text",
                }, path
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name in {"sqlite3", "psycopg", "psycopg2"}
                    for alias in node.names
                ), path

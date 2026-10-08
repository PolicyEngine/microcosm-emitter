"""Package metadata and installed entry points use the requested public names."""

import tomllib
from importlib.metadata import distribution, entry_points
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAME = "microcosm-emitter"


def test_single_distribution_with_required_telemetry_dependencies():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = config["project"]
    assert project["name"] == EXPECTED_NAME
    assert project["urls"]["Source"] == (
        "https://github.com/PolicyEngine/microcosm-emitter"
    )
    assert not list((ROOT / "packages").glob("*/pyproject.toml"))
    assert "workspace" not in config.get("tool", {}).get("uv", {})
    assert "optional-dependencies" not in project
    assert set(project["dependencies"]) == {
        "alembic>=1.13.3,<2",
        "sqlalchemy>=2,<3",
        "huggingface-hub>=0.20,<2",
        "psutil>=6,<8",
    }


def test_installed_distribution_includes_host_telemetry_and_migrations():
    emitter = distribution(EXPECTED_NAME)
    assert emitter.version == "0.1.0"
    assert emitter.metadata.get_all("Project-URL") == [
        "Source, https://github.com/PolicyEngine/microcosm-emitter"
    ]
    requirements = emitter.requires
    assert all("extra ==" not in requirement for requirement in requirements)
    assert not any("microcosm-provider" in requirement for requirement in requirements)
    modules = list(entry_points(group="microcosm.emitter.modules", name="telemetry"))
    assert len(modules) == 1
    assert modules[0].value == "microcosm_emitter.telemetry.service:create_module"
    assert find_spec("microcosm_emitter.host.__main__") is not None
    assert find_spec("microcosm_emitter.telemetry.client") is not None
    assert (
        find_spec(
            "microcosm_emitter.telemetry.service.alembic.versions.v20261007_01_initial_spool"
        )
        is not None
    )

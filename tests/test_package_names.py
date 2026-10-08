"""Package metadata and installed entry points use the requested public names."""

import tomllib
from importlib.metadata import distribution, entry_points
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAMES = {"microcosm-provider-client", "microcosm-provider-telemetry"}


def test_distribution_names_and_source_links():
    projects = [
        tomllib.loads(path.read_text())["project"]
        for path in (ROOT / "packages").glob("*/pyproject.toml")
    ]
    assert {project["name"] for project in projects} == EXPECTED_NAMES
    for project in projects:
        assert project["urls"]["Source"] == (
            "https://github.com/PolicyEngine/microcosm-local-provider"
        )


def test_installed_names_dependency_and_module_registration():
    for name in EXPECTED_NAMES:
        assert distribution(name).version == "0.1.0"
    telemetry = distribution("microcosm-provider-telemetry")
    assert "microcosm-provider-client==0.1.0" in telemetry.requires
    modules = list(entry_points(group="microcosm.provider.modules", name="telemetry"))
    assert len(modules) == 1
    assert modules[0].value == "microcosm_provider_telemetry.service:create_module"

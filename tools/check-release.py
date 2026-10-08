"""Refuse tags that do not describe both distribution versions."""

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(tag: str) -> None:
    projects = [
        tomllib.loads(path.read_text())["project"]
        for path in sorted((ROOT / "packages").glob("*/pyproject.toml"))
    ]
    assert len(projects) == 4
    assert all(tag == f"v{project['version']}" for project in projects), (
        "Release tag must match both distributions"
    )
    reader = json.loads((ROOT / "typescript/orrery/package.json").read_text())
    assert tag == f"v{reader['version']}", "Release tag must match the reader package"
    telemetry = next(
        project
        for project in projects
        if project["name"] == "microcosm-provider-telemetry"
    )
    assert (
        f"microcosm-provider-client=={telemetry['version']}"
        in telemetry["dependencies"]
    )


if __name__ == "__main__":
    validate(sys.argv[1])

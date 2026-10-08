"""Refuse tags that do not describe the emitter distribution version."""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(tag: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "microcosm-emitter"
    assert tag == f"v{project['version']}", "Release tag must match the emitter version"


if __name__ == "__main__":
    validate(sys.argv[1])

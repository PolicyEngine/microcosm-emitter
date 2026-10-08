"""Validate release ordering, credential scope, and script-only shell logic."""

import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def test_shell_logic_lives_in_scripts():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        config = workflow(path.name)
        for job in config["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    assert "\n" not in step["run"].strip(), (path, step)


def test_release_publishes_only_verified_artifacts_with_oidc():
    config = workflow("publish.yml")
    assert config["on"]["release"]["types"] == ["published"]
    jobs = config["jobs"]
    assert jobs["verify"]["needs"] == "release"
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["publish"]["needs"] == "verify"
    assert jobs["publish"]["environment"] == "pypi"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert "id-token" not in config["permissions"]
    assert jobs["publish"]["steps"][0]["with"]["name"] == "distributions"
    assert not any("run" in step for step in jobs["publish"]["steps"])
    assert "secrets." not in (ROOT / ".github/workflows/publish.yml").read_text()


def test_all_required_checks_include_real_collector_and_matrix():
    config = workflow("ci.yml")
    assert "pull_request" in config["on"]
    jobs = config["jobs"]
    assert jobs["required"]["if"] == "always()"
    assert set(jobs["required"]["needs"]) == {
        "quality",
        "tests",
        "artifacts",
        "collector",
        "reader",
    }
    assert jobs["tests"]["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python": ["3.13", "3.14"],
    }
    assert jobs["collector"]["needs"] == "artifacts"
    assert jobs["collector"]["env"]["TEST_DATABASE_URL"].endswith("/telemetry_test")


def test_release_tag_matches_both_package_versions():
    validate = runpy.run_path(str(ROOT / "tools/check-release.py"))["validate"]
    validate("v0.1.0")
    with pytest.raises(AssertionError, match="Release tag"):
        validate("v9.9.9")


def test_reader_publishes_verified_tarball_after_python_with_oidc():
    jobs = workflow("publish.yml")["jobs"]
    reader = jobs["publish-reader"]
    assert reader["needs"] == ["verify", "publish"]
    assert reader["environment"] == "pypi"
    assert reader["permissions"]["id-token"] == "write"
    download = next(
        s for s in reader["steps"] if "download-artifact@" in s.get("uses", "")
    )
    assert download["with"]["name"] == "orrery-reader"
    assert reader["steps"][-1]["run"] == "bash tools/publish-reader.sh"
    assert (
        workflow("ci.yml")["jobs"]["reader"]["steps"][-1]["with"]["name"]
        == "orrery-reader"
    )

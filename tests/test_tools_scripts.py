"""The tools/ scripts that CI and the release run.

Each test runs a script with fake uv, uvx and git first on PATH, in a
temporary directory, so nothing is built, installed, tagged or pushed.
"""

import re
import shutil
import subprocess

import pytest
import yaml
from fake_commands import (
    TOOLS_DIRECTORY,
    WORKFLOWS_DIRECTORY,
    FakeCommands,
    run_script,
)

# A loopback address with synthetic credentials; nothing listens on it.
TEST_DATABASE_URL = "postgresql+psycopg://user:password@127.0.0.1:1/telemetry_test"


@pytest.fixture
def fakes(tmp_path):
    return FakeCommands.create(tmp_path)


@pytest.fixture
def checkout(tmp_path):
    directory = tmp_path / "checkout"
    directory.mkdir()
    return directory


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def with_matches_sorted(call, matches_from):
    """Return ``call`` with its arguments from ``matches_from`` on sorted.

    They are a glob's matches, which bash orders by the locale's collation:
    macOS runners use en_US.UTF-8 and Ubuntu's C, and no command here
    depends on the order.
    """
    return [*call[:matches_from], *sorted(call[matches_from:])]


# --- check-quality.sh -----------------------------------------------------


def check_quality(fakes, checkout, uv_body=""):
    fakes.add("uv", uv_body)
    return run_script("check-quality.sh", env=fakes.env(), cwd=checkout)


def test_check_quality_runs_ruff_in_the_locked_environment(fakes, checkout):
    write(checkout / "tools/sample.sh", "#!/usr/bin/env bash\necho sample\n")

    result = check_quality(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["uv", "run", "--locked", "ruff", "check", "."],
        ["uv", "run", "--locked", "ruff", "format", "--check", "."],
    ]


def test_check_quality_fails_on_a_script_that_does_not_parse(fakes, checkout):
    write(checkout / "tools/broken.sh", "if then\n")

    result = check_quality(fakes, checkout)

    assert result.returncode != 0
    assert "broken.sh" in result.stderr


def test_check_quality_stops_when_ruff_check_fails(fakes, checkout):
    result = check_quality(
        fakes, checkout, uv_body='[[ "$4" == "check" ]] && exit 1\nexit 0\n'
    )

    assert result.returncode == 1
    assert fakes.calls() == [["uv", "run", "--locked", "ruff", "check", "."]]


# --- check-changelog.sh ---------------------------------------------------


def test_check_changelog_checks_the_fragments_with_bump_version(fakes, checkout):
    fakes.add("uv")

    result = run_script("check-changelog.sh", env=fakes.env(), cwd=checkout)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["uv", "run", "--no-project", "python", "tools/bump-version.py", "--check"]
    ]


def test_check_changelog_fails_when_bump_version_refuses(fakes, checkout):
    fakes.add("uv", "exit 1\n")

    result = run_script("check-changelog.sh", env=fakes.env(), cwd=checkout)

    assert result.returncode == 1


# --- check-artifacts.sh ---------------------------------------------------

# uv build writes one wheel and one source archive.
FAKE_UV_BUILD = """\
if [[ "$1" == "build" ]]; then
  mkdir -p dist
  touch dist/example-1.0-py3-none-any.whl dist/example-1.0.tar.gz
fi
exit "${FAKE_UV_EXIT:-0}"
"""


def check_artifacts(fakes, checkout, **env):
    """Run a copy in ``checkout``: the script works in the directory above it."""
    (checkout / "tools").mkdir()
    shutil.copy(TOOLS_DIRECTORY / "check-artifacts.sh", checkout / "tools")
    fakes.add("uv", FAKE_UV_BUILD)
    return subprocess.run(
        ["bash", str(checkout / "tools/check-artifacts.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=fakes.env(**env),
        cwd=checkout,
    )


def test_check_artifacts_builds_once_then_checks_and_installs_the_build(
    fakes, checkout
):
    write(checkout / "dist/stale-0.9-py3-none-any.whl")

    result = check_artifacts(fakes, checkout)

    assert result.returncode == 0, result.stderr
    build, twine, installed_check = fakes.calls()
    assert build == ["uv", "build", "--all-packages", "--out-dir", "dist"]
    assert with_matches_sorted(twine, 6) == [
        "uv",
        "run",
        "--locked",
        "twine",
        "check",
        "--strict",
        "dist/example-1.0-py3-none-any.whl",
        "dist/example-1.0.tar.gz",
    ]
    assert installed_check == [
        "uv",
        "run",
        "--locked",
        "python",
        "tools/check-installed.py",
    ]
    assert not (checkout / "dist/stale-0.9-py3-none-any.whl").exists()


def test_check_artifacts_stops_when_the_build_fails(fakes, checkout):
    result = check_artifacts(fakes, checkout, FAKE_UV_EXIT="1")

    assert result.returncode == 1
    assert fakes.calls() == [["uv", "build", "--all-packages", "--out-dir", "dist"]]


# --- check-collector.sh ---------------------------------------------------


def pinned_collector_commit():
    """Return the commit ci.yml's collector job checks out into .collector."""
    steps = yaml.load(
        (WORKFLOWS_DIRECTORY / "ci.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["collector"]["steps"]
    (checkout_step,) = [
        step for step in steps if step.get("with", {}).get("path") == ".collector"
    ]
    return checkout_step["with"]["ref"]


def check_collector(fakes, checkout, collector_head, **env):
    fakes.add("git", f'echo "{collector_head}"\n')
    fakes.add("uv")
    write(checkout / "dist/microcosm_emitter-1.0-py3-none-any.whl")
    write(checkout / "dist/microcosm_emitter_interface-1.0-py3-none-any.whl")
    return run_script("check-collector.sh", env=fakes.env(**env), cwd=checkout)


def test_check_collector_pins_the_commit_ci_checks_out():
    script = (TOOLS_DIRECTORY / "check-collector.sh").read_text()

    (expected,) = re.findall(r"^expected_commit=(\w+)$", script, re.MULTILINE)
    assert expected == pinned_collector_commit()


def test_check_collector_runs_the_contract_test_against_the_built_wheels(
    fakes, checkout
):
    result = check_collector(
        fakes,
        checkout,
        pinned_collector_commit(),
        TEST_DATABASE_URL=TEST_DATABASE_URL,
    )

    assert result.returncode == 0, result.stderr
    calls = fakes.calls()
    calls[3] = with_matches_sorted(calls[3], 5)
    assert calls == [
        ["git", "-C", ".collector", "rev-parse", "HEAD"],
        ["uv", "sync", "--locked"],
        ["uv", "pip", "install", "-e", ".collector/telemetry-service"],
        [
            "uv",
            "pip",
            "install",
            "--reinstall",
            "--no-deps",
            "dist/microcosm_emitter-1.0-py3-none-any.whl",
            "dist/microcosm_emitter_interface-1.0-py3-none-any.whl",
        ],
        ["uv", "run", "--no-sync", "pytest", "tests/test_collector_contract.py"],
    ]


def test_check_collector_refuses_a_collector_at_another_commit(fakes, checkout):
    result = check_collector(
        fakes, checkout, "0" * 40, TEST_DATABASE_URL=TEST_DATABASE_URL
    )

    assert result.returncode != 0
    assert fakes.calls("uv") == []


def test_check_collector_requires_the_test_database(fakes, checkout):
    environment_without_database = {"TEST_DATABASE_URL": ""}

    result = check_collector(
        fakes, checkout, pinned_collector_commit(), **environment_without_database
    )

    assert result.returncode != 0
    assert "Set the disposable PostgreSQL test database URL" in result.stderr
    assert fakes.calls() == []


# --- publish-git-tag.sh ---------------------------------------------------

RELEASE_COMMIT = "1111111111111111111111111111111111111111"
OTHER_COMMIT = "2222222222222222222222222222222222222222"
# The fake git answers HEAD with FAKE_HEAD, says whether the tag exists with
# FAKE_TAG_EXISTS_EXIT (1: it does not), and names the tag's commit with
# FAKE_TAG_COMMIT.
FAKE_GIT_FOR_TAGS = """\
if [[ "$1" == "rev-parse" ]]; then
  case "$2" in
    HEAD) echo "${FAKE_HEAD}" ;;
    --verify) exit "${FAKE_TAG_EXISTS_EXIT:-1}" ;;
    *) echo "${FAKE_TAG_COMMIT}" ;;
  esac
fi
"""


def publish_git_tag(fakes, checkout, **env):
    write(checkout / "pyproject.toml", '[project]\nname = "x"\nversion = "1.2.3"\n')
    fakes.add("git", FAKE_GIT_FOR_TAGS)
    return run_script(
        "publish-git-tag.sh",
        env=fakes.env(FAKE_HEAD=RELEASE_COMMIT, **env),
        cwd=checkout,
    )


def test_publish_git_tag_tags_the_release_commit_with_its_version(fakes, checkout):
    result = publish_git_tag(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", "--verify", "--quiet", "refs/tags/1.2.3"],
        ["git", "tag", "1.2.3"],
        ["git", "push", "origin", "refs/tags/1.2.3"],
    ]


def test_publish_git_tag_accepts_the_tag_on_the_same_commit(fakes, checkout):
    """A rerun after a later publication failure finds the tag it made."""
    result = publish_git_tag(
        fakes, checkout, FAKE_TAG_EXISTS_EXIT="0", FAKE_TAG_COMMIT=RELEASE_COMMIT
    )

    assert result.returncode == 0, result.stderr
    assert "Tag 1.2.3 already exists." in result.stdout
    assert not any(call[1] in ("tag", "push") for call in fakes.calls())


def test_publish_git_tag_refuses_a_tag_on_another_commit(fakes, checkout):
    result = publish_git_tag(
        fakes, checkout, FAKE_TAG_EXISTS_EXIT="0", FAKE_TAG_COMMIT=OTHER_COMMIT
    )

    assert result.returncode == 1
    assert f"Tag 1.2.3 identifies {OTHER_COMMIT}" in result.stderr
    assert not any(call[1] in ("tag", "push") for call in fakes.calls())


# --- update-version.sh ----------------------------------------------------


# uv run prints the version bump-version.py would choose.
FAKE_UV_BUMP = 'if [[ "$1" == "run" ]]; then echo 1.2.4; fi\n'


def update_version(fakes, checkout, uv_body=FAKE_UV_BUMP):
    """Run the release script with one pending fragment, as a merge has."""
    write(checkout / "changelog.d/change.fixed.md", "A fix.\n")
    fakes.add("uv", uv_body)
    fakes.add("uvx")
    return run_script("update-version.sh", env=fakes.env(), cwd=checkout)


def test_update_version_bumps_builds_the_changelog_and_relocks(fakes, checkout):
    result = update_version(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["uv", "run", "--no-project", "python", "tools/bump-version.py"],
        [
            "uvx",
            "--from",
            "towncrier==26.9.0",
            "towncrier",
            "build",
            "--yes",
            "--version",
            "1.2.4",
        ],
        ["uv", "lock"],
    ]


def test_update_version_stops_when_the_bump_fails(fakes, checkout):
    result = update_version(fakes, checkout, uv_body="exit 1\n")

    assert result.returncode != 0
    assert fakes.calls("uvx") == []

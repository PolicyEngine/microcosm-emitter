"""Dependabot relocks uv.lock, and a lock-only merge releases nothing.

Dependabot runs on GitHub's side with its own credentials and opens one
grouped pull request a week that changes only uv.lock. Such a pull request
needs no changelog fragment, since it changes no published package's
requirements, and its merge makes no release. The script tests run each
script with fake git, uv and uvx first on PATH.
"""

import pytest
import yaml
from fake_commands import (
    REPOSITORY_ROOT,
    WORKFLOWS_DIRECTORY,
    FakeCommands,
    github_output,
    run_script,
)


@pytest.fixture
def fakes(tmp_path):
    return FakeCommands.create(tmp_path)


@pytest.fixture
def checkout(tmp_path):
    directory = tmp_path / "checkout"
    (directory / "changelog.d").mkdir(parents=True)
    (directory / "changelog.d/.gitkeep").touch()
    return directory


def workflow(name):
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser, and
    # builds only strings, lists and mappings.
    return yaml.load(
        (WORKFLOWS_DIRECTORY / name).read_text(),
        Loader=yaml.BaseLoader,  # noqa: S506
    )


def test_dependabot_proposes_uv_lock_updates_weekly():
    config = yaml.safe_load((REPOSITORY_ROOT / ".github/dependabot.yml").read_text())

    assert config["version"] == 2
    (update,) = config["updates"]
    assert update["package-ecosystem"] == "uv"
    assert update["directory"] == "/"
    assert update["schedule"] == {"interval": "weekly"}
    # One pull request that changes only uv.lock, never pyproject.toml's ranges.
    assert update["versioning-strategy"] == "lockfile-only"
    assert update["groups"] == {"locked-dependencies": {"patterns": ["*"]}}


# --- check-changelog.sh ---------------------------------------------------

DEPENDABOT = "dependabot[bot]"
# The fake git lists FAKE_CHANGED as the files the pull request changes.
FAKE_GIT = 'if [[ "$1" == "diff" ]]; then printf "%b" "${FAKE_CHANGED:-}"; fi\n'
FRAGMENT_CHECK = [
    "uv",
    "run",
    "--no-project",
    "python",
    "tools/bump-version.py",
    "--check",
]


def check_changelog(fakes, checkout, **env):
    fakes.add("git", FAKE_GIT)
    fakes.add("uv", 'exit "${FAKE_UV_EXIT:-0}"\n')
    return run_script("check-changelog.sh", env=fakes.env(**env), cwd=checkout)


def test_a_dependabot_lock_update_needs_no_fragment(fakes, checkout):
    result = check_changelog(
        fakes,
        checkout,
        PR_AUTHOR=DEPENDABOT,
        BASE_REF="main",
        FAKE_CHANGED="uv.lock\\n",
        FAKE_UV_EXIT="1",
    )

    assert result.returncode == 0, result.stderr
    assert "changes only uv.lock" in result.stdout
    assert fakes.calls() == [
        ["git", "diff", "--name-only", "origin/main...HEAD"],
    ]


@pytest.mark.parametrize(
    ("author", "changed"),
    [
        (DEPENDABOT, "pyproject.toml\\nuv.lock\\n"),
        ("someone", "uv.lock\\n"),
    ],
    ids=["dependabot-changing-a-range", "a-person"],
)
def test_other_pull_requests_need_a_fragment(fakes, checkout, author, changed):
    result = check_changelog(
        fakes,
        checkout,
        PR_AUTHOR=author,
        BASE_REF="main",
        FAKE_CHANGED=changed,
        FAKE_UV_EXIT="1",
    )

    assert result.returncode == 1
    assert fakes.calls("uv") == [FRAGMENT_CHECK]


def test_the_changelog_job_tells_the_script_who_opened_the_pull_request():
    changelog = workflow("pr.yaml")["jobs"]["changelog"]
    # The script compares the pull request with its base branch's history.
    assert changelog["steps"][0]["with"]["fetch-depth"] == "0"
    check = changelog["steps"][-1]
    assert check["run"] == "bash tools/check-changelog.sh"
    assert check["env"] == {
        "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
        "BASE_REF": "${{ github.base_ref }}",
    }


# --- update-version.sh and push.yaml -------------------------------------


def update_version(fakes, checkout):
    output = checkout.parent / "github-output"
    fakes.add("uv", 'if [[ "$1" == "run" ]]; then echo 1.2.4; fi\n')
    fakes.add("uvx")
    result = run_script(
        "update-version.sh", env=fakes.env(GITHUB_OUTPUT=str(output)), cwd=checkout
    )
    return result, github_output(output)


def test_a_merge_without_a_fragment_releases_nothing(fakes, checkout):
    result, output = update_version(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert "releases nothing" in result.stdout
    assert output == {"released": "false"}
    assert fakes.calls() == []


def test_a_merge_with_a_fragment_is_released(fakes, checkout):
    (checkout / "changelog.d/lock.changed.md").write_text("A change.\n")

    result, output = update_version(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert output == {"released": "true"}
    assert [call[0] for call in fakes.calls()] == ["uv", "uvx", "uv"]


def test_versioning_commits_only_a_release():
    steps = workflow("push.yaml")["jobs"]["versioning"]["steps"]
    bump, commit = steps[-2], steps[-1]
    assert bump["id"] == "version"
    assert bump["run"] == "bash tools/update-version.sh"
    assert commit["uses"].startswith("EndBug/add-and-commit@")
    # Without the version commit, the release jobs, which run on it, never start.
    assert commit["if"] == "steps.version.outputs.released == 'true'"

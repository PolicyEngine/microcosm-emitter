"""Validate release ordering, credential scope and workflow structure."""

import re
import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "Update microcosm-emitter"
VERSIONED_FILE = "pyproject.toml"
INTERFACE_VERSIONED_FILE = "packages/microcosm-emitter-interface/pyproject.toml"
VERSIONED_FILES = (VERSIONED_FILE, INTERFACE_VERSIONED_FILE)


def workflow_paths():
    return sorted((ROOT / ".github/workflows").glob("*.y*ml"))


def workflow(name):
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


@pytest.fixture
def bump_version():
    return runpy.run_path(str(ROOT / "tools/bump-version.py"))


def release_tree(root, *fragments):
    for versioned_file in VERSIONED_FILES:
        (root / versioned_file).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / versioned_file, root / versioned_file)
    (root / "changelog.d").mkdir()
    (root / "changelog.d/.gitkeep").touch()
    for fragment in fragments:
        (root / "changelog.d" / fragment).write_text("A change.\n")
    return root


def remove_version_line(pyproject_text):
    return re.sub(
        r'^version = "[^"]+"\n', "", pyproject_text, count=1, flags=re.MULTILINE
    )


def add_tool_version_line(pyproject_text):
    return pyproject_text + '\n[tool.example]\nversion = "9.9.9"\n'


def test_pull_requests_require_every_check_and_a_changelog_fragment():
    config = workflow("pr.yaml")
    assert "pull_request" in config["on"]
    jobs = config["jobs"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["changelog"]["steps"][-1]["run"] == "bash tools/check-changelog.sh"
    required = jobs["required"]
    assert required["name"] == "Required checks"
    assert required["if"] == "always()"
    assert set(required["needs"]) == {"lint-workflows", "changelog", "verify", "rules"}
    every_need_succeeded = " ".join("success" for _ in required["needs"])
    assert required["steps"][-1]["run"] == f'test "$RESULTS" = "{every_need_succeeded}"'


def test_pull_requests_check_the_rules_registry():
    rules = workflow("pr.yaml")["jobs"]["rules"]
    assert rules["name"] == "Rules registry"
    # The guard reads the base branch's registry from history, and looks up
    # the tracking issue of each [rule.downgrade] with the job's token.
    assert rules["permissions"] == {"contents": "read", "issues": "read"}
    checkout = rules["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == "0"
    runs = [step["run"] for step in rules["steps"] if "run" in step]
    assert runs[-2:] == [
        "uv run --no-project python -m unittest discover -s scripts/rules/tests -t .",
        "uv run --no-project python scripts/run_quality_guards.py",
    ]
    assert rules["steps"][-1]["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_pull_request_descriptions_are_checked_for_pull_requests_into_main():
    """A workflow of its own, so editing a description reruns only this check."""
    config = workflow("pr-description.yaml")
    assert config["on"] == {
        "pull_request": {
            "branches": ["main"],
            "types": ["opened", "edited", "reopened", "synchronize"],
        }
    }
    assert config["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    (job_id,) = config["jobs"]
    assert job_id == "pr-description"
    job = config["jobs"][job_id]
    # main's ruleset requires the check by this name.
    assert job["name"] == "PR description"
    # GitHub also sends "edited" for closed pull requests, whose issue merging
    # closed.
    assert job["if"] == "${{ github.event.pull_request.state == 'open' }}"
    check = job["steps"][-1]
    assert check["name"] == "Check the first line and the tracking issue"
    assert check["run"] == "uv run --no-project python -m scripts.rules.pr_description"
    assert check["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_verification_includes_real_collector_and_matrix():
    config = workflow("ci.yml")
    assert set(config["on"]) == {"workflow_call"}
    jobs = config["jobs"]
    assert set(jobs) == {"quality", "tests", "build-packages", "collector"}
    assert jobs["tests"]["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python": ["3.13", "3.14"],
    }
    assert jobs["collector"]["needs"] == "build-packages"
    assert jobs["collector"]["env"]["TEST_DATABASE_URL"].endswith("/telemetry_test")


def test_release_commit_is_verified_then_tagged_then_published():
    config = workflow("push.yaml")
    assert config["on"]["push"]["branches"] == ["main"]
    jobs = config["jobs"]
    assert set(jobs) == {"versioning", "verify", "tag", "publish"}
    assert f"!= '{RELEASE_COMMIT}'" in jobs["versioning"]["if"]
    assert jobs["versioning"]["steps"][-2]["run"] == "bash tools/update-version.sh"
    assert jobs["versioning"]["steps"][-1]["with"]["message"] == RELEASE_COMMIT
    assert f"== '{RELEASE_COMMIT}'" in jobs["verify"]["if"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["tag"]["needs"] == "verify"
    assert jobs["tag"]["steps"][-1]["run"] == "bash tools/publish-git-tag.sh"
    assert jobs["publish"]["needs"] == "tag"


def test_only_publication_holds_oidc_and_only_versioning_holds_secrets():
    config = workflow("push.yaml")
    assert "id-token" not in config["permissions"]
    jobs = config["jobs"]
    oidc_jobs = {
        name for name, job in jobs.items() if "id-token" in job.get("permissions", {})
    }
    assert oidc_jobs == {"publish"}
    assert jobs["publish"]["environment"] == "pypi"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    text = (ROOT / ".github/workflows/push.yaml").read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"APP_ID", "APP_PRIVATE_KEY"}
    assert text.count("secrets.") == 2


#: The artifacts the verification workflow uploads for each distribution, in
#: the order PyPI must receive them: the emitter requires the interface of
#: the same version, so the interface is uploaded first.
PUBLISHED_ARTIFACTS = (
    ("interface-distribution", "dist/microcosm_emitter_interface-*"),
    ("emitter-distribution", "dist/microcosm_emitter-*"),
)


def uploaded_artifacts():
    steps = workflow("ci.yml")["jobs"]["build-packages"]["steps"]
    return {
        step["with"]["name"]: step["with"]
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    }


def test_verification_uploads_each_distribution_as_its_own_artifact():
    artifacts = uploaded_artifacts()
    # The collector job installs both wheels from the combined artifact.
    assert artifacts["distributions"]["path"] == "dist/*"
    for name, path in PUBLISHED_ARTIFACTS:
        assert artifacts[name]["path"] == path
        assert artifacts[name]["if-no-files-found"] == "error"


def test_emitter_artifact_pattern_excludes_the_interface():
    from fnmatch import fnmatch

    emitter_pattern = dict(PUBLISHED_ARTIFACTS)["emitter-distribution"]
    interface_pattern = dict(PUBLISHED_ARTIFACTS)["interface-distribution"]
    emitter_files = [
        "dist/microcosm_emitter-1.0.0-py3-none-any.whl",
        "dist/microcosm_emitter-1.0.0.tar.gz",
    ]
    interface_files = [
        "dist/microcosm_emitter_interface-1.0.0-py3-none-any.whl",
        "dist/microcosm_emitter_interface-1.0.0.tar.gz",
    ]
    assert all(fnmatch(name, emitter_pattern) for name in emitter_files)
    assert not any(fnmatch(name, emitter_pattern) for name in interface_files)
    assert all(fnmatch(name, interface_pattern) for name in interface_files)
    assert not any(fnmatch(name, interface_pattern) for name in emitter_files)


def test_pypi_receives_only_verified_artifacts():
    publish = workflow("push.yaml")["jobs"]["publish"]
    downloads = [
        step["with"]["name"]
        for step in publish["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert downloads == [name for name, _ in PUBLISHED_ARTIFACTS]
    assert set(downloads) <= set(uploaded_artifacts())
    assert not any("run" in step for step in publish["steps"])


def test_publication_uploads_the_interface_first_and_can_be_rerun():
    """A failed or partial upload must leave a release that a rerun completes.

    The interface goes first, so PyPI never offers an emitter whose required
    interface is missing. Files already on PyPI are skipped, so rerunning the
    job after a partial upload uploads only what is missing instead of
    stopping on "File already exists".
    """
    steps = workflow("push.yaml")["jobs"]["publish"]["steps"]
    sequence = []
    for step in steps:
        uses = step.get("uses", "")
        if uses.startswith("actions/download-artifact@"):
            sequence.append(("download", step["with"]["name"], step["with"]["path"]))
        elif uses.startswith("pypa/gh-action-pypi-publish@"):
            assert step["with"]["skip-existing"] == "true"
            sequence.append(("upload", step["with"]["packages-dir"]))
    assert sequence == [
        ("download", "interface-distribution", "dist/interface"),
        ("upload", "dist/interface/"),
        ("download", "emitter-distribution", "dist/emitter"),
        ("upload", "dist/emitter/"),
    ]


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        ({"fixed"}, "1.4.3"),
        ({"fixed", "changed"}, "1.4.3"),
        ({"fixed", "added"}, "1.5.0"),
        ({"removed"}, "1.5.0"),
        ({"added", "breaking"}, "2.0.0"),
    ],
)
def test_fragment_types_choose_the_bump(bump_version, types, expected):
    assert bump_version["next_version"]("1.4.2", types) == expected


def changed_lines(versioned_file, root):
    original = (ROOT / versioned_file).read_text().splitlines()
    updated = (root / versioned_file).read_text().splitlines()
    assert len(updated) == len(original)
    return [
        (original_line, updated_line)
        for original_line, updated_line in zip(original, updated, strict=True)
        if original_line != updated_line
    ]


def test_release_rewrites_both_versions_and_the_interface_pin_only(
    tmp_path, bump_version
):
    root = release_tree(tmp_path, "1.added.md", "repair.fixed.md")
    current, upcoming = bump_version["plan_release"](root)
    assert upcoming == bump_version["next_version"](current, {"added", "fixed"})
    bump_version["write_version"](root, current, upcoming)
    assert bump_version["current_version"](root) == upcoming
    assert changed_lines(VERSIONED_FILE, root) == [
        (f'version = "{current}"', f'version = "{upcoming}"'),
        (
            f'    "microcosm-emitter-interface=={current}",',
            f'    "microcosm-emitter-interface=={upcoming}",',
        ),
    ]
    assert changed_lines(INTERFACE_VERSIONED_FILE, root) == [
        (f'version = "{current}"', f'version = "{upcoming}"')
    ]


def test_release_refuses_diverged_distribution_versions(tmp_path, bump_version):
    root = release_tree(tmp_path, "1.fixed.md")
    interface = root / INTERFACE_VERSIONED_FILE
    interface.write_text(
        re.sub(
            r'^version = "[^"]+"$',
            'version = "9.9.9"',
            interface.read_text(),
            count=1,
            flags=re.MULTILINE,
        )
    )
    with pytest.raises(ValueError, match="same version"):
        bump_version["plan_release"](root)


def test_release_refuses_an_interface_pin_that_differs_from_the_version(
    tmp_path, bump_version
):
    root = release_tree(tmp_path, "1.fixed.md")
    pyproject = root / VERSIONED_FILE
    pyproject.write_text(
        re.sub(
            r'"microcosm-emitter-interface==[^"]+"',
            '"microcosm-emitter-interface>=0.1"',
            pyproject.read_text(),
        )
    )
    with pytest.raises(ValueError, match="pin"):
        bump_version["plan_release"](root)


@pytest.mark.parametrize(
    "fragments", [[], ["1.feature.md"], ["1.added.rst"], ["notes.md"]]
)
def test_release_refuses_missing_or_untyped_fragments(
    tmp_path, bump_version, fragments
):
    root = release_tree(tmp_path, *fragments)
    with pytest.raises(ValueError, match="changelog fragment"):
        bump_version["plan_release"](root)


@pytest.mark.parametrize("versioned_file", VERSIONED_FILES)
@pytest.mark.parametrize(
    "edit_pyproject",
    [remove_version_line, add_tool_version_line],
    ids=["missing", "duplicated"],
)
def test_release_refuses_a_missing_or_duplicated_version_line(
    tmp_path, bump_version, edit_pyproject, versioned_file
):
    root = release_tree(tmp_path, "1.fixed.md")
    pyproject = root / versioned_file
    pyproject.write_text(edit_pyproject(pyproject.read_text()))
    with pytest.raises(ValueError, match="one version line"):
        bump_version["plan_release"](root)


def test_build_verifies_both_distributions_from_one_script():
    script = (ROOT / "tools/check-artifacts.sh").read_text()
    assert script.count("uv build ") == 1
    assert "uv build --all-packages --out-dir dist" in script
    # Each package's build would clear the shared output directory, so one
    # build could delete the other's artifacts; clear it once, beforehand.
    assert "--clear" not in script
    assert script.index("rm -rf dist") < script.index("uv build ")
    for path in workflow_paths():
        assert "uv build" not in path.read_text()


def test_installed_check_keeps_the_interface_apart_from_the_emitter():
    script = (ROOT / "tools/check-installed.py").read_text()
    assert "microcosm_emitter_interface-" in script
    assert "tools/installed-interface-smoke.py" in script

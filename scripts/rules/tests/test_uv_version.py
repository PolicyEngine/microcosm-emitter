"""Tests for the uv-version guard: every uv install pins the one uv release.

The release is written once, as the line uv==X.Y.Z in .github/uv-version.txt,
which setup-uv steps read with version-file. The root pyproject.toml's
[tool.uv] required-version is a lower bound that release meets, so a
developer's older uv refuses the project and Dependabot's uv, another
release, still runs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.rules.tests.support import REPOSITORY_TABLE
from scripts.rules.uv_version import ROOT_PROJECT, VERSION_FILE, run_check, scan

FIXTURES = Path(__file__).parent / "fixtures" / "uv_version"
PINNED = "0.13.6"
UV_VERSION_REGISTRY = (
    """
schema_version = 1
repo = "example"
"""
    + REPOSITORY_TABLE.replace('rule_prefix = "ex"', 'rule_prefix = "emitter"')
    + """
[[category]]
id = "dependencies"
title = "Dependencies"

[[rule]]
id = "emitter.dependencies.uv-version-pinned"
category = "dependencies"
statement = "Every uv install pins the one uv release."
rationale = "README.md"
scope = ["**"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "uv-version"
"""
)


RELEASE_LINE = f"uv=={PINNED}"
FLOOR = ">=0.13"


def write_files(
    root: Path,
    files: dict[str, str],
    release: str | None = RELEASE_LINE,
    required: str | None = FLOOR,
) -> None:
    """Write each file under ``root``, plus the version file and the root project.

    The version file holds ``release`` and the root project's required-version is
    ``required``; either file is left out when its value is ``None``.
    """
    if release is not None:
        (root / VERSION_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / VERSION_FILE).write_text(f"{release}\n", encoding="utf-8")
    if required is not None:
        (root / ROOT_PROJECT).write_text(
            f'[tool.uv]\nrequired-version = "{required}"\n', encoding="utf-8"
        )
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


class GuardCase(unittest.TestCase):
    """Runs the guard on files written to a temporary directory."""

    def messages(
        self,
        files: dict[str, str],
        release: str | None = RELEASE_LINE,
        required: str | None = FLOOR,
    ) -> list[str]:
        """Return the guard's violation messages for ``files``."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_files(root, files, release, required)
        return [violation.message for violation in run_check(root)]

    def versions(self, files: dict[str, str]) -> list[tuple[str, str | None]]:
        """Return each install the scan finds, as (site, pinned version)."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_files(root, files)
        return [(install.site, install.version) for install in scan(root)]


class TestUvVersionProof(unittest.TestCase):
    def test_rule_proof_uv_version_pinned(self) -> None:
        bad = [violation.message for violation in run_check(FIXTURES / "bad")]
        good = run_check(FIXTURES / "good")

        self.assertEqual(
            bad,
            [
                (
                    ".github/workflows/ci.yml:16: astral-sh/setup-uv step does not pin "
                    "a uv release"
                ),
                (
                    ".github/workflows/ci.yml:31: astral-sh/setup-uv step writes uv "
                    f"0.13.4 itself instead of reading {VERSION_FILE}"
                ),
                (
                    "Dockerfile:5: uv container image does not pin a uv release (it "
                    "names latest)"
                ),
                "app/modal_app.py:9: Modal uv_sync call does not pin a uv release",
                "scripts/install-uv.sh:6: uv download URL does not pin a uv release",
                (
                    "scripts/install-uv.sh:7: install of the uv package does not pin a "
                    "uv release"
                ),
            ],
        )
        self.assertEqual(good, [])


class TestRuleId(unittest.TestCase):
    def test_violations_name_the_rule_that_lists_the_guard(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_files(
            root,
            {
                "rules/registry.toml": UV_VERSION_REGISTRY,
                "Dockerfile": "COPY --from=ghcr.io/astral-sh/uv /uv /bin/\n",
            },
        )

        self.assertEqual(
            [violation.rule_id for violation in run_check(root)],
            ["emitter.dependencies.uv-version-pinned"],
        )

    def test_without_a_registry_violations_name_no_rule(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_files(
            root, {"Dockerfile": "COPY --from=ghcr.io/astral-sh/uv /uv /bin/\n"}
        )

        self.assertEqual([violation.rule_id for violation in run_check(root)], [None])


class TestVersionFile(GuardCase):
    """The release is the one line uv==X.Y.Z of .github/uv-version.txt."""

    def test_one_release_passes(self) -> None:
        self.assertEqual(self.messages({}), [])

    def test_comment_lines_are_ignored(self) -> None:
        self.assertEqual(
            self.messages({}, release=f"# The uv release CI installs.\n{RELEASE_LINE}"),
            [],
        )

    def test_a_missing_version_file_fails(self) -> None:
        self.assertEqual(
            self.messages({}, release=None),
            [f"{VERSION_FILE}, which holds the uv release, is missing"],
        )

    def test_anything_but_one_release_fails(self) -> None:
        for release in ("uv>=0.13.6", "uv==0.13", "0.13.6", "uv==0.13.6\nuv==0.13.4"):
            with self.subTest(release=release):
                self.assertEqual(
                    self.messages({}, release=release),
                    [f"{VERSION_FILE} does not hold one line uv==X.Y.Z"],
                )

    def test_unpinned_installs_are_reported_even_without_a_release(self) -> None:
        self.assertEqual(
            self.messages(
                {"Dockerfile": "COPY --from=ghcr.io/astral-sh/uv /uv /bin/\n"},
                release=None,
            ),
            [
                f"{VERSION_FILE}, which holds the uv release, is missing",
                "Dockerfile:1: uv container image does not pin a uv release",
            ],
        )


class TestRequiredVersion(GuardCase):
    """The root project's required-version is a lower bound the release meets."""

    def test_a_floor_the_release_meets_passes(self) -> None:
        for required in (">=0.13", f">={PINNED}", ">=0.12.4"):
            with self.subTest(required=required):
                self.assertEqual(self.messages({}, required=required), [])

    def test_an_exact_release_fails(self) -> None:
        """Dependabot runs uv lock with its own uv, which stops on another."""
        self.assertEqual(
            self.messages({}, required=f"=={PINNED}"),
            [
                (
                    f"pyproject.toml's [tool.uv] required-version is '=={PINNED}', not "
                    "a lower bound written >=X.Y"
                )
            ],
        )

    def test_a_floor_above_the_release_fails(self) -> None:
        self.assertEqual(
            self.messages({}, required=">=0.14"),
            [
                (
                    "pyproject.toml's [tool.uv] required-version '>=0.14' refuses uv "
                    f"{PINNED}, the release in {VERSION_FILE}"
                )
            ],
        )

    def test_another_form_fails(self) -> None:
        for required in ("~=0.13", "<0.14", ">=0.13,<0.14", "0.13.6"):
            with self.subTest(required=required):
                self.assertEqual(
                    self.messages({}, required=required),
                    [
                        (
                            "pyproject.toml's [tool.uv] required-version is "
                            f"{required!r}, not a lower bound written >=X.Y"
                        )
                    ],
                )

    def test_a_missing_required_version_fails(self) -> None:
        self.assertEqual(
            self.messages({"pyproject.toml": '[project]\nname = "example"\n'}),
            ["pyproject.toml sets no [tool.uv] required-version"],
        )

    def test_a_repository_without_a_root_project_needs_none(self) -> None:
        self.assertEqual(self.messages({}, required=None), [])


class TestSetupUvSteps(GuardCase):
    def workflow(self, steps: str) -> dict[str, str]:
        """Return a one-job workflow file holding ``steps``."""
        return {
            ".github/workflows/ci.yml": (
                "name: CI\non: pull_request\njobs:\n  build:\n"
                "    runs-on: ubuntu-latest\n    steps:\n" + steps
            )
        }

    def test_the_version_input_is_read_from_the_step_it_belongs_to(self) -> None:
        installs = self.versions(
            self.workflow(
                "      - name: Install uv\n"
                "        id: uv\n"
                "        uses: astral-sh/setup-uv@v8.1.0\n"
                "        with:\n"
                "          enable-cache: true\n"
                "          version: '0.13.6'\n"
                "      - uses: astral-sh/setup-uv@v8.1.0  # no version\n"
                "      - name: Next step\n"
                "        with:\n"
                "          version: 0.13.4\n"
                "        run: echo next\n"
            )
        )

        self.assertEqual(
            installs,
            [
                (".github/workflows/ci.yml:9: astral-sh/setup-uv step", "0.13.6"),
                (".github/workflows/ci.yml:13: astral-sh/setup-uv step", None),
            ],
        )

    def test_a_step_written_as_one_item_line_is_read(self) -> None:
        installs = self.versions(
            self.workflow(
                "    - uses: astral-sh/setup-uv@0123456789abcdef  # v8.1.0\n"
                '      with: { enable-cache: true, version: "0.13.6" }\n'
            )
        )

        self.assertEqual(
            installs, [(".github/workflows/ci.yml:7: astral-sh/setup-uv step", PINNED)]
        )

    def test_reading_the_version_file_pins_the_release(self) -> None:
        self.assertEqual(
            self.messages(
                self.workflow(
                    "    - uses: astral-sh/setup-uv@v8.1.0\n"
                    "      with:\n"
                    f"        version-file: {VERSION_FILE}\n"
                    "    - uses: astral-sh/setup-uv@v8.1.0\n"
                    f"      with: {{ version-file: {VERSION_FILE} }}\n"
                )
            ),
            [],
        )

    def test_another_version_file_is_not_a_pin(self) -> None:
        """pyproject.toml's required-version is a floor, not a pinned release.

        setup-uv would meet it with the newest release.
        """
        messages = self.messages(
            self.workflow(
                "    - uses: astral-sh/setup-uv@v8.1.0\n"
                "      with:\n"
                "        version-file: pyproject.toml\n"
            )
        )

        self.assertEqual(
            messages,
            [
                (
                    ".github/workflows/ci.yml:7: astral-sh/setup-uv step does not pin "
                    "a uv release"
                )
            ],
        )

    def test_a_step_that_writes_a_release_itself_is_reported(self) -> None:
        """Even the right release: it is written once, in the root project."""
        messages = self.messages(
            self.workflow(
                "    - uses: astral-sh/setup-uv@v8.1.0\n"
                "      with:\n"
                "        version: 0.13.4\n"
                "    - uses: astral-sh/setup-uv@v8.1.0\n"
                "      with:\n"
                f"        version: {PINNED}\n"
                "    - uses: astral-sh/setup-uv@v8.1.0\n"
                "      with:\n"
                "        version: latest\n"
            )
        )

        self.assertEqual(
            messages,
            [
                (
                    ".github/workflows/ci.yml:7: astral-sh/setup-uv step writes uv "
                    f"0.13.4 itself instead of reading {VERSION_FILE}"
                ),
                (
                    ".github/workflows/ci.yml:10: astral-sh/setup-uv step writes uv "
                    f"{PINNED} itself instead of reading {VERSION_FILE}"
                ),
                (
                    ".github/workflows/ci.yml:13: astral-sh/setup-uv step does not pin "
                    "a uv release (it names latest)"
                ),
            ],
        )

    def test_composite_actions_are_read(self) -> None:
        messages = self.messages(
            {
                ".github/actions/setup/action.yml": (
                    "runs:\n  using: composite\n  steps:\n"
                    "    - uses: astral-sh/setup-uv@v8.1.0\n"
                )
            }
        )

        self.assertEqual(
            messages,
            [
                (
                    ".github/actions/setup/action.yml:4: astral-sh/setup-uv step does "
                    "not pin a uv release"
                )
            ],
        )


class TestImagesAndDownloads(GuardCase):
    def test_image_tags_must_name_the_release(self) -> None:
        digest = "0" * 64
        messages = self.messages(
            {
                "Dockerfile": (
                    "FROM ghcr.io/astral-sh/uv:0.13.6-python3.13-bookworm AS uv\n"
                    f"COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:{digest} "
                    "/uv /bin/\n"
                    "COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/\n"
                    "COPY --from=docker.io/astral/uv:python3.13-bookworm /uv /bin/\n"
                    "# COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/\n"
                ),
            }
        )

        self.assertEqual(
            messages,
            [
                "Dockerfile:3: uv container image pins uv 0.13.4, not 0.13.6",
                (
                    "Dockerfile:4: uv container image does not pin a uv release (it "
                    "names python3.13-bookworm)"
                ),
            ],
        )

    def test_install_scripts_and_release_downloads_must_name_the_release(
        self,
    ) -> None:
        installs = self.versions(
            {
                "Makefile": (
                    "install-uv:\n"
                    "\tcurl -LsSf https://astral.sh/uv/0.13.6/install.sh | sh\n"
                    "\tcurl -LsSf https://astral.sh/uv/install.sh | sh\n"
                    "\tcurl -LO https://github.com/astral-sh/uv/releases/download/"
                    "0.13.6/uv.tar.gz\n"
                    "\tcurl -LO https://github.com/astral-sh/uv/releases/latest/"
                    "download/uv.tar.gz\n"
                ),
            }
        )

        self.assertEqual(
            installs,
            [
                ("Makefile:2: uv download URL", PINNED),
                ("Makefile:3: uv download URL", None),
                ("Makefile:4: uv download URL", PINNED),
                ("Makefile:5: uv download URL", None),
            ],
        )

    def test_package_installs_of_uv_are_found_and_other_packages_are_not(
        self,
    ) -> None:
        installs = self.versions(
            {
                "install.sh": (
                    "pip install uv\n"
                    'python -m pip install --upgrade "uv==0.13.6"\n'
                    "pipx install uv==0.13.4\n"
                    "uv tool install ruff==0.15.22\n"
                    "pip install uvicorn requests\n"
                    "pip install requests && uv sync --frozen\n"
                    "uv self update\n"
                    "uv self update 0.13.6\n"
                ),
            }
        )

        self.assertEqual(
            installs,
            [
                ("install.sh:1: install of the uv package", None),
                ("install.sh:2: install of the uv package", PINNED),
                ("install.sh:3: install of the uv package", "0.13.4"),
                ("install.sh:7: uv self update", None),
                ("install.sh:8: uv self update", PINNED),
            ],
        )


class TestModalImages(GuardCase):
    def test_modal_uv_installs_must_pass_the_release(self) -> None:
        messages = self.messages(
            {
                "app/modal_app.py": (
                    "import modal\n"
                    "\n"
                    "# .uv_sync( written in a comment is no call.\n"
                    "pinned = modal.Image.debian_slim().uv_sync(\n"
                    '    uv_project_dir=".", frozen=True, uv_version="0.13.6"\n'
                    ")\n"
                    'other = modal.Image.debian_slim().uv_sync(uv_version="0.13.4")\n'
                    'unpinned = modal.Image.debian_slim().uv_pip_install("requests")\n'
                    'tool = modal.Image.debian_slim().pip_install("uv")\n'
                    "commands = modal.Image.debian_slim().run_commands(\n"
                    '    "curl -LsSf https://astral.sh/uv/install.sh | sh"\n'
                    ")\n"
                ),
            }
        )

        self.assertEqual(
            messages,
            [
                "app/modal_app.py:7: Modal uv_sync call pins uv 0.13.4, not 0.13.6",
                (
                    "app/modal_app.py:8: Modal uv_pip_install call does not pin a uv "
                    "release"
                ),
                (
                    "app/modal_app.py:9: install of the uv package does not pin a uv "
                    "release"
                ),
                "app/modal_app.py:10: uv download URL does not pin a uv release",
            ],
        )

    def test_test_modules_and_fixtures_are_not_read(self) -> None:
        messages = self.messages(
            {
                "rules/registry.toml": UV_VERSION_REGISTRY,
                "app/tests/test_image.py": "image.uv_sync(frozen=True)\n",
                "app/fixtures/fake_modal.py": "image.uv_sync(frozen=True)\n",
            }
        )

        self.assertEqual(messages, [])

    def test_test_modules_are_the_registry_test_directories(self) -> None:
        files = {"app/checks/test_image.py": "image.uv_sync(frozen=True)\n"}

        self.assertEqual(len(self.messages(files)), 1)
        self.assertEqual(
            self.messages(
                {
                    **files,
                    "rules/registry.toml": UV_VERSION_REGISTRY.replace(
                        'test_directories = ["tests"]', 'test_directories = ["checks"]'
                    ),
                }
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

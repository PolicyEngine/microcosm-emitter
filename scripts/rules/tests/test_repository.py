"""Tests for the ``[repository]`` table: where the checks find what they read.

The table names what differs between repositories that use this tool: the
prefix of every rule id, the pull-request workflow, the directories that hold
scripts and tests, the package directories, and the directories where a ruff
per-file ignore may switch rules off.
"""

from __future__ import annotations

import unittest

from scripts.rules.load import load_text
from scripts.rules.model import Repository
from scripts.rules.tests.support import REGISTRY, REPOSITORY_TABLE, TODAY, Repo
from scripts.rules.validate import validate

REGISTRY_WITHOUT_TABLE = REGISTRY.replace(REPOSITORY_TABLE, "")
SETTINGS = (
    "rule_prefix",
    "pr_workflow",
    "script_directories",
    "test_directories",
    "packages",
    "file_exception_directories",
)


def schema_problems(text: str) -> list[str]:
    """Return what loading the registry ``text`` reports."""
    _, problems = load_text(text)
    return [str(problem) for problem in problems]


def validation_messages(files: dict[str, str]) -> list[str]:
    """Return what validating the registry in a repository with ``files`` reports."""
    with Repo(files, render=False) as repo:
        registry, problems = load_text(repo.read("rules/registry.toml"))
        assert registry is not None and not problems, problems
        return [str(violation) for violation in validate(registry, repo.root, TODAY)]


class LoadTest(unittest.TestCase):
    def test_the_settings_are_read(self) -> None:
        registry, problems = load_text(REGISTRY)

        self.assertEqual(problems, [])
        assert registry is not None
        self.assertEqual(
            registry.repository,
            Repository(
                rule_prefix="ex",
                pr_workflow=".github/workflows/pr.yml",
                script_directories=("scripts",),
                test_directories=("tests",),
                packages=(".",),
                file_exception_directories=("tests", ".github", "scripts"),
            ),
        )

    def test_the_table_is_required(self) -> None:
        self.assertEqual(
            schema_problems(REGISTRY_WITHOUT_TABLE),
            [
                (
                    "[registry] schema: the registry has no [repository] table. Fix: "
                    "add [repository] with rule_prefix, pr_workflow, "
                    "script_directories, test_directories, packages and "
                    "file_exception_directories."
                ),
            ],
        )

    def test_every_setting_is_required(self) -> None:
        for setting in SETTINGS:
            with self.subTest(setting=setting):
                lines = [
                    line
                    for line in REPOSITORY_TABLE.splitlines()
                    if not line.startswith(f"{setting} =")
                ]
                text = REGISTRY.replace(REPOSITORY_TABLE, "\n".join(lines) + "\n")

                problems = schema_problems(text)

                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"repository.{setting} must be a non-empty", problems[0])

    def test_unknown_settings_are_reported(self) -> None:
        text = REGISTRY.replace(
            REPOSITORY_TABLE, REPOSITORY_TABLE + 'scripts_dir = "scripts"\n'
        )

        self.assertEqual(
            schema_problems(text),
            [
                (
                    "[registry] schema: unknown key 'scripts_dir' in repository. Fix: "
                    "remove it or use one of ['file_exception_directories', "
                    "'packages', 'pr_workflow', 'rule_prefix', 'script_directories', "
                    "'test_directories']."
                ),
            ],
        )

    def test_the_rule_prefix_is_one_lowercase_word(self) -> None:
        for prefix in ("Ex", "ex.layers", "ex_1", "-ex"):
            with self.subTest(prefix=prefix):
                text = REGISTRY.replace(
                    'rule_prefix = "ex"', f'rule_prefix = "{prefix}"'
                )

                self.assertEqual(
                    schema_problems(text),
                    [
                        (
                            f"[registry] schema: repository.rule_prefix is {prefix!r}, "
                            "not one lowercase word. Fix: use the word every rule id "
                            "starts with, such as 'emitter'."
                        ),
                    ],
                )

    def test_discovery_reads_the_pr_workflow_unless_it_names_workflows(self) -> None:
        text = REGISTRY.replace(
            'pr_workflow = ".github/workflows/pr.yml"',
            'pr_workflow = ".github/workflows/pr.yaml"',
        )
        registry, _ = load_text(text)
        assert registry is not None
        self.assertEqual(registry.discovery.workflows, (".github/workflows/pr.yaml",))

        named = text.replace(
            "[discovery]\n", '[discovery]\nworkflows = [".github/workflows/x.yml"]\n'
        )
        registry, _ = load_text(named)
        assert registry is not None
        self.assertEqual(registry.discovery.workflows, (".github/workflows/x.yml",))

    def test_a_rule_id_is_the_prefix_and_a_name(self) -> None:
        repository = Repository(rule_prefix="emitter")

        self.assertEqual(
            repository.rule_id("dependencies.lock-committed"),
            "emitter.dependencies.lock-committed",
        )


class ValidateTest(unittest.TestCase):
    def test_the_example_repository_passes(self) -> None:
        self.assertEqual(validation_messages({}), [])

    def test_every_rule_id_starts_with_the_prefix(self) -> None:
        registry = REGISTRY.replace(
            'id = "ex.quality.types"', 'id = "other.quality.types"'
        )

        self.assertEqual(
            validation_messages({"rules/registry.toml": registry}),
            [
                (
                    "[other.quality.types] schema: id does not start with 'ex.', the "
                    "registry's rule_prefix. Fix: rename the rule to start with 'ex.'."
                ),
            ],
        )

    def test_the_paths_the_settings_name_exist(self) -> None:
        registry = (
            REGISTRY.replace(
                'pr_workflow = ".github/workflows/pr.yml"',
                'pr_workflow = ".github/workflows/pull-request.yaml"',
            )
            .replace(
                'script_directories = ["scripts"]',
                'script_directories = ["scripts", "tools"]',
            )
            .replace('packages = ["."]', 'packages = [".", "packages/*"]')
        )

        messages = validation_messages({"rules/registry.toml": registry})

        self.assertEqual(
            [message for message in messages if "repository." in message],
            [
                (
                    "[registry] schema: repository.pr_workflow "
                    ".github/workflows/pull-request.yaml does not exist. Fix: name the "
                    "workflow that runs on every pull request."
                ),
                (
                    "[registry] schema: repository.script_directories names tools, "
                    "which is not a directory. Fix: list the directories that hold "
                    "the repository's scripts."
                ),
                (
                    "[registry] schema: repository.packages pattern 'packages/*' "
                    "matches no directory with a pyproject.toml. Fix: list the "
                    "package directories, with . for the repository root."
                ),
            ],
        )

    def test_package_patterns_match_the_root_and_subdirectories(self) -> None:
        registry = REGISTRY.replace(
            'packages = ["."]', 'packages = [".", "packages/*"]'
        )
        files = {
            "rules/registry.toml": registry,
            "packages/interface/pyproject.toml": "",
        }

        self.assertEqual(validation_messages(files), [])


if __name__ == "__main__":
    unittest.main()

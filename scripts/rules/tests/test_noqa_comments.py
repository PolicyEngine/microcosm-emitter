"""Tests for the noqa-comments guard: the number of inline suppressions may not grow."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from collections import Counter
from pathlib import Path

from scripts.rules.baseline import check_baselines
from scripts.rules.load import load_text
from scripts.rules.noqa_comments import (
    REPO_ROOT,
    comment_entries,
    compare,
    find_entries,
    run_check,
)
from scripts.rules.tests.support import REPOSITORY_TABLE

FIXTURES = Path(__file__).parent / "fixtures" / "noqa_comments"
BASELINE_FILE = "rules/baselines/noqa-comments.txt"


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write each ``path: text`` pair under ``root``."""
    for relative_path, text in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def violations_for(files: dict[str, str], baseline: list[str]) -> list[str]:
    """Write ``files`` into a temporary directory; compare them with ``baseline``."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_files(root, files)
        found = find_entries(root, sorted(files))
    return [
        str(violation)
        for violation in compare(found, Counter(baseline), None, BASELINE_FILE)
    ]


def ratcheted_registry(count: int):
    """Return a registry whose noqa-comments rule tolerates ``count`` comments."""
    registry, problems = load_text(
        """
schema_version = 1
repo = "example"
"""
        + REPOSITORY_TABLE
        + f"""
[[category]]
id = "quality"
title = "Quality"

[[rule]]
id = "ex.quality.inline-suppressions"
category = "quality"
statement = "The number of inline suppressions may not grow."
rationale = "docs/guide.md"
scope = ["**/*.py"]
tier = "mechanical"
status = "ratcheted"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "noqa-comments"
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "quality-guards"
invocation = "run_quality_guards.py"
[rule.baseline]
file = "{BASELINE_FILE}"
count = {count}
"""
    )
    assert registry is not None, problems
    return registry


def base_branch_violations(base_count: int, head_count: int) -> list[str]:
    """Compare a head baseline count with the base branch's, as the guard does."""
    violations, _ = check_baselines(
        ratcheted_registry(head_count), ratcheted_registry(base_count)
    )
    return [str(violation) for violation in violations]


def fixture_violations(fixture_name: str) -> list[str]:
    """Run the guard's comparison on a proof fixture manifest."""
    manifest = tomllib.loads((FIXTURES / fixture_name).read_text())
    files = {str(entry["path"]): str(entry["text"]) for entry in manifest["file"]}
    return violations_for(files, [str(entry) for entry in manifest["baseline"]])


class TestNoqaCommentsProof(unittest.TestCase):
    """Proof: the guard rejects an unlisted suppression and accepts listed ones."""

    def test_rule_proof_inline_suppressions_only_shrink(self) -> None:
        self.assertEqual(
            fixture_violations("bad.toml"),
            [
                (
                    "[registry] noqa-comments: inline suppression not in the "
                    "baseline: pkg/cli.py: E501, T201. Fix: fix the finding instead "
                    "of suppressing it, or remove another suppression and list this "
                    "one in rules/baselines/noqa-comments.txt: the total may not grow."
                ),
                (
                    "[registry] noqa-comments: inline suppression not in the "
                    "baseline: pkg/service.py: BLE001. Fix: fix the finding instead "
                    "of suppressing it, or remove another suppression and list this "
                    "one in rules/baselines/noqa-comments.txt: the total may not grow."
                ),
            ],
        )
        self.assertEqual(fixture_violations("good.toml"), [])


class TestNewAndRemovedSuppressions(unittest.TestCase):
    def test_a_new_noqa_comment_fails(self) -> None:
        files = {
            "pkg/module.py": ("import os  # noqa: F401\nimport sys  # noqa: F401\n")
        }

        self.assertEqual(
            violations_for(files, ["pkg/module.py: F401"]),
            [
                (
                    "[registry] noqa-comments: inline suppression not in the "
                    "baseline: pkg/module.py: F401. Fix: fix the finding instead of "
                    "suppressing it, or remove another suppression and list this one "
                    "in rules/baselines/noqa-comments.txt: the total may not grow."
                )
            ],
        )

    def test_a_removed_noqa_comment_asks_for_the_baseline_to_shrink(self) -> None:
        files = {"pkg/module.py": "import os  # noqa: F401\n"}

        self.assertEqual(
            violations_for(files, ["pkg/module.py: F401", "pkg/module.py: F401"]),
            [
                (
                    "[registry] noqa-comments: baseline entry no longer applies: "
                    "pkg/module.py: F401 is listed 2 times, but the file has 1 such "
                    "comment. Fix: remove the extra entry from "
                    "rules/baselines/noqa-comments.txt and lower the rule's baseline "
                    "count."
                )
            ],
        )

    def test_a_suppression_moved_within_its_file_still_matches(self) -> None:
        files = {"pkg/module.py": "VALUE = 1\n\n\nimport os  # noqa: E402\n"}

        self.assertEqual(violations_for(files, ["pkg/module.py: E402"]), [])


class TestCommentEntries(unittest.TestCase):
    def test_each_directive_is_one_entry_with_its_codes_sorted(self) -> None:
        source = (
            "def start(self, a, b, c, d, e, f):  # noqa: PLR0917, PLR0913\n"
            "    print(a)  # noqa:T201\n"
            "    return b  # A reason first.  # noqa: RET504\n"
        )

        self.assertEqual(
            comment_entries("pkg/module.py", source),
            [
                "pkg/module.py: PLR0913, PLR0917",
                "pkg/module.py: T201",
                "pkg/module.py: RET504",
            ],
        )

    def test_a_blanket_noqa_is_an_entry_without_codes(self) -> None:
        self.assertEqual(
            comment_entries("pkg/module.py", "import os  # NOQA\n"),
            ["pkg/module.py: (all)"],
        )

    def test_text_that_only_mentions_noqa_is_not_a_suppression(self) -> None:
        source = (
            'HELP = "add # noqa: T201 to keep a print"\n'
            "# A comment that names noqa is not a directive.\n"
        )

        self.assertEqual(comment_entries("pkg/module.py", source), [])


class TestRuffSuppressionComments(unittest.TestCase):
    """Ruff's own suppression comments are suppressions too, labelled by form."""

    def test_a_ruff_ignore_comment_is_an_entry(self) -> None:
        source = (
            "try:\n    run()\nexcept Exception:  # ruff: ignore[BLE001]\n    pass\n"
        )

        self.assertEqual(
            comment_entries("pkg/module.py", source),
            ["pkg/module.py: BLE001 (ruff: ignore)"],
        )

    def test_a_ruff_disable_range_is_one_entry_and_its_enable_none(self) -> None:
        source = (
            "# ruff: disable[BLE001, S110]\n"
            "try:\n    run()\nexcept Exception:\n    pass\n"
            "# ruff: enable[BLE001, S110]\n"
        )

        self.assertEqual(
            comment_entries("pkg/module.py", source),
            ["pkg/module.py: BLE001, S110 (ruff: disable)"],
        )

    def test_a_ruff_file_ignore_comment_is_an_entry(self) -> None:
        self.assertEqual(
            comment_entries("pkg/module.py", "# ruff: file-ignore[T201]\nprint(1)\n"),
            ["pkg/module.py: T201 (ruff: file-ignore)"],
        )

    def test_a_file_level_noqa_on_its_own_line_is_an_entry(self) -> None:
        source = "# ruff: noqa: T201\n# ruff: noqa\n# flake8: noqa\nprint(1)\n"

        self.assertEqual(
            comment_entries("pkg/module.py", source),
            [
                "pkg/module.py: T201 (ruff: noqa)",
                "pkg/module.py: (all) (ruff: noqa)",
                "pkg/module.py: (all) (flake8: noqa)",
            ],
        )

    def test_a_file_level_noqa_after_code_is_not_applied_or_counted(self) -> None:
        # Ruff warns that a file-level suppression must be on its own line, and
        # applies nothing.
        self.assertEqual(
            comment_entries("pkg/module.py", "print(1)  # ruff: noqa: T201\n"), []
        )


class TestCodesAsRuffReadsThem(unittest.TestCase):
    def test_codes_written_without_a_delimiter_are_separate_codes(self) -> None:
        self.assertEqual(
            comment_entries("pkg/module.py", "run()  # noqa: BLE001S110\n"),
            ["pkg/module.py: BLE001, S110"],
        )

    def test_a_space_before_the_colon_still_names_the_codes(self) -> None:
        self.assertEqual(
            comment_entries("pkg/module.py", "run()  # noqa : BLE001\n"),
            ["pkg/module.py: BLE001"],
        )

    def test_lowercase_codes_suppress_nothing_and_are_not_counted(self) -> None:
        # Ruff warns that the directive is invalid and reports the finding.
        self.assertEqual(
            comment_entries("pkg/module.py", "run()  # noqa: ble001\n"), []
        )


class TestTheTotalMayNotGrow(unittest.TestCase):
    """The base branch comparison counts comments only, as Anthony decided.

    A pull request may add a suppression only by removing another, so the
    same total passes even when a code is added to a comment, a code is
    swapped for another or a file is renamed, as long as the baseline is
    updated to match; a higher total fails.
    """

    def test_a_code_added_to_an_existing_comment_passes_at_the_same_total(
        self,
    ) -> None:
        files = {"pkg/module.py": "run()  # noqa: BLE001, S110\n"}

        self.assertEqual(violations_for(files, ["pkg/module.py: BLE001, S110"]), [])
        self.assertEqual(base_branch_violations(1, 1), [])

    def test_a_swapped_code_passes_at_the_same_total(self) -> None:
        files = {"pkg/module.py": "run()  # noqa: S110\n"}

        self.assertEqual(violations_for(files, ["pkg/module.py: S110"]), [])
        self.assertEqual(base_branch_violations(1, 1), [])

    def test_a_renamed_file_passes_at_the_same_total(self) -> None:
        files = {"pkg/renamed.py": "run()  # noqa: BLE001\n"}

        self.assertEqual(violations_for(files, ["pkg/renamed.py: BLE001"]), [])
        self.assertEqual(base_branch_violations(1, 1), [])

    def test_a_higher_total_fails_against_the_base_branch(self) -> None:
        self.assertEqual(
            base_branch_violations(1, 2),
            [
                (
                    "[ex.quality.inline-suppressions] baseline: baseline grew from 1 "
                    "to 2. Fix: fix the new violations instead of adding them to the "
                    "baseline."
                )
            ],
        )

    def test_a_comment_moved_between_two_versions_of_a_file_keeps_its_entry(
        self,
    ) -> None:
        before = "import os  # noqa: F401\n\nVALUE = 1\n"
        after = "VALUE = 1\n\n\nimport os  # noqa: F401\n"

        self.assertEqual(
            Counter(comment_entries("pkg/module.py", before)),
            Counter(comment_entries("pkg/module.py", after)),
        )


class TestThisRepository(unittest.TestCase):
    def test_the_baseline_lists_every_noqa_comment_in_this_repository(self) -> None:
        self.assertEqual([str(violation) for violation in run_check(REPO_ROOT)], [])


if __name__ == "__main__":
    unittest.main()

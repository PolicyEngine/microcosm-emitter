"""Tests for the noqa-comments guard: inline suppressions may only become fewer."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from collections import Counter
from pathlib import Path

from scripts.rules.noqa_comments import (
    REPO_ROOT,
    comment_entries,
    compare,
    find_entries,
    run_check,
)

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
                    "of suppressing it; rules/baselines/noqa-comments.txt may only "
                    "shrink."
                ),
                (
                    "[registry] noqa-comments: inline suppression not in the "
                    "baseline: pkg/service.py: BLE001. Fix: fix the finding instead "
                    "of suppressing it; rules/baselines/noqa-comments.txt may only "
                    "shrink."
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
                    "suppressing it; rules/baselines/noqa-comments.txt may only "
                    "shrink."
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


class TestThisRepository(unittest.TestCase):
    def test_the_baseline_lists_every_noqa_comment_in_this_repository(self) -> None:
        self.assertEqual([str(violation) for violation in run_check(REPO_ROOT)], [])


if __name__ == "__main__":
    unittest.main()

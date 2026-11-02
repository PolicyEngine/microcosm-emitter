"""Tests for the pull-request description check.

The check reads the description from the event payload and looks the issue up
with ``scripts.rules.issues``, whose own tests cover the lookup. Here the lookup
is replaced in-process, except in one test that runs the whole check through a
fake ``gh``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.rules import pr_description
from scripts.rules.issues import IssueLookupError, NotAnOpenIssueError
from scripts.rules.pr_description import (
    DescriptionError,
    issue_number_from_description,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "pr_description"
REPOSITORY = "example/repo"
RULE = "emitter.process.pr-tracking-issue"


def description_error(body: str | None) -> str:
    """Return the message the check gives for ``body``, which it must reject."""
    with unittest.TestCase().assertRaises(DescriptionError) as raised:
        issue_number_from_description(body)
    return str(raised.exception)


class TestPrDescriptionProof(unittest.TestCase):
    """Proof: PR #13's description, which named no issue, fails; #38's passes."""

    def test_rule_proof_pr_tracking_issue(self) -> None:
        bad = json.loads((FIXTURES / "bad_event.json").read_text())
        good = json.loads((FIXTURES / "good_event.json").read_text())

        with self.assertRaises(DescriptionError):
            issue_number_from_description(bad["pull_request"]["body"])
        self.assertEqual(
            issue_number_from_description(good["pull_request"]["body"]), 37
        )


class FirstLineTest(unittest.TestCase):
    def test_a_first_line_of_fixes_and_a_number_names_the_issue(self) -> None:
        for body in ("Fixes #42", "Fixes #42\n\nWhat this does.", "Fixes #7\n"):
            with self.subTest(body=body):
                self.assertEqual(
                    issue_number_from_description(body), int(body[7:].split()[0])
                )

    def test_crlf_and_carriage_return_line_endings_end_the_first_line(self) -> None:
        """The GitHub web interface saves descriptions with CRLF."""
        for body in ("Fixes #42\r\n\r\nWhat this does.", "Fixes #42\rWhat this does."):
            with self.subTest(body=body):
                self.assertEqual(issue_number_from_description(body), 42)

    def test_trailing_spaces_and_tabs_after_the_number_are_ignored(self) -> None:
        self.assertEqual(issue_number_from_description("Fixes #42 \t\nBody."), 42)

    def test_an_empty_or_missing_description_is_rejected(self) -> None:
        for body in (None, "", "  \n\t\n"):
            with self.subTest(body=body):
                self.assertTrue(
                    description_error(body).startswith("The description is empty.")
                )

    def test_a_fixes_line_that_is_not_first_is_rejected(self) -> None:
        message = description_error("What this does.\n\nFixes #42\n")

        self.assertTrue(
            message.startswith("Fixes #42 is on line 3, but it must be the first line.")
        )

    def test_a_blank_line_before_the_fixes_line_is_rejected(self) -> None:
        self.assertIn("is on line 2", description_error("\nFixes #42"))

    def test_leading_whitespace_is_rejected(self) -> None:
        """Four leading spaces make a Markdown code block, where GitHub reads no
        closing keyword."""
        for body in ("    Fixes #42", " Fixes #42", "\tFixes #42"):
            with self.subTest(body=body):
                self.assertTrue(
                    description_error(body).startswith(
                        "The first line starts with whitespace"
                    )
                )

    def test_other_first_lines_are_rejected(self) -> None:
        for first_line in (
            "Fixes #042",
            "Fixes #0",
            "fixes #42",
            "Fixes 42",
            "Fixes #42.",
            "Fixes #42, #43",
            "Fixes: #42",
            "Closes #42",
            "Fixes PolicyEngine/microcosm-emitter#42",
            "Fixes #42 and the release",
        ):
            with self.subTest(first_line=first_line):
                self.assertEqual(
                    description_error(first_line + "\n\nBody.").split(". Edit")[0],
                    f'The first line is "{first_line}"',
                )

    def test_a_long_first_line_is_shortened_in_the_message(self) -> None:
        message = description_error("word " * 40)

        self.assertIn('The first line is "' + ("word " * 16).rstrip() + '..."', message)

    def test_the_message_says_how_to_fix_the_description(self) -> None:
        self.assertIn(
            "Edit the description so its first line is exactly Fixes #ISSUE_NUMBER, "
            "where ISSUE_NUMBER is an open issue in this repository; editing the "
            "description runs this check again",
            description_error("Body."),
        )


class MainTest(unittest.TestCase):
    """main() reads the event, looks the issue up and prints one annotation."""

    def run_main(
        self,
        body: object,
        lookup: mock.Mock | None = None,
        event: dict[str, object] | None = None,
    ) -> tuple[int, str, mock.Mock]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        event_path = directory / "event.json"
        event_path.write_text(
            json.dumps(event if event is not None else {"pull_request": {"body": body}})
        )
        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_EVENT_PATH": str(event_path),
        }
        lookup = lookup or mock.Mock(return_value=None)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(pr_description, "require_open_issue", lookup),
            redirect_stdout(output),
        ):
            status = main()
        return status, output.getvalue(), lookup

    def test_a_description_naming_an_open_issue_passes(self) -> None:
        status, output, lookup = self.run_main("Fixes #42\n\nBody.")

        self.assertEqual(status, 0)
        lookup.assert_called_once_with(REPOSITORY, 42)
        self.assertEqual(
            output,
            "The first line is Fixes #42, and #42 is an open issue in example/repo.\n",
        )

    def test_a_dependabot_pull_request_needs_no_tracking_issue(self) -> None:
        """Dependabot writes its own descriptions and cannot name an issue."""
        event = {
            "pull_request": {
                "body": "Bumps the locked-dependencies group with 3 updates.",
                "user": {"login": "dependabot[bot]"},
            }
        }

        status, output, lookup = self.run_main(None, event=event)

        self.assertEqual(status, 0)
        lookup.assert_not_called()
        self.assertEqual(
            output,
            "Dependabot opened this pull request, so its description needs no "
            "tracking issue.\n",
        )

    def test_another_author_is_checked(self) -> None:
        for login in ("someone", "github-actions[bot]", "dependabot"):
            event = {"pull_request": {"body": "Body.", "user": {"login": login}}}
            with self.subTest(login=login):
                status, output, _lookup = self.run_main(None, event=event)

                self.assertEqual(status, 1)
                self.assertIn('The first line is "Body."', output)

    def test_a_rejected_first_line_fails_without_asking_github(self) -> None:
        status, output, lookup = self.run_main("Body.\n")

        self.assertEqual(status, 1)
        lookup.assert_not_called()
        self.assertTrue(
            output.startswith(
                '::error title=PR description::The first line is "Body.". '
            )
        )
        self.assertIn(f"(rule {RULE})", output)

    def test_a_null_description_fails(self) -> None:
        status, output, _lookup = self.run_main(None)

        self.assertEqual(status, 1)
        self.assertIn("The description is empty.", output)

    def test_a_closed_issue_says_to_reopen_it_or_name_another(self) -> None:
        closed = mock.Mock(
            side_effect=NotAnOpenIssueError("Issue #42 is closed.", state="closed")
        )

        status, output, _lookup = self.run_main("Fixes #42", closed)

        self.assertEqual(status, 1)
        self.assertIn(
            "Issue #42 is closed. If this pull request still fixes it,", output
        )

    def test_a_number_naming_no_issue_says_how_to_fix_the_description(self) -> None:
        missing = mock.Mock(
            side_effect=NotAnOpenIssueError("#42 is a pull request, not an issue.")
        )

        status, output, _lookup = self.run_main("Fixes #42", missing)

        self.assertEqual(status, 1)
        self.assertIn(
            "#42 is a pull request, not an issue. Edit the description", output
        )

    def test_a_failed_lookup_says_to_run_the_job_again(self) -> None:
        failed = mock.Mock(side_effect=IssueLookupError("Could not look up #42."))

        status, output, _lookup = self.run_main("Fixes #42", failed)

        self.assertEqual(status, 1)
        self.assertIn("Could not look up #42. Re-run this job", output)

    def test_the_annotation_stays_on_one_line(self) -> None:
        failed = mock.Mock(side_effect=IssueLookupError("first\nsecond 100%"))

        _status, output, _lookup = self.run_main("Fixes #42", failed)

        self.assertEqual(output.count("\n"), 1)
        self.assertIn("first%0Asecond 100%25", output)

    def test_the_check_requires_a_pull_request_event(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_main(None, event={"push": {}})

        self.assertIn("has no pull_request", str(raised.exception))

    def test_the_check_requires_its_environment(self) -> None:
        for variable in ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH"):
            environment = {
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_EVENT_PATH": str(FIXTURES / "good_event.json"),
                variable: "",
            }
            with (
                self.subTest(variable=variable),
                mock.patch.dict(os.environ, environment),
                self.assertRaises(SystemExit) as raised,
            ):
                main()
            self.assertEqual(str(raised.exception), f"{variable} is required")


class CommandTest(unittest.TestCase):
    def test_the_check_runs_as_a_module_and_asks_gh_about_the_issue(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_directory = directory / "bin"
        bin_directory.mkdir()
        fake_gh = bin_directory / "gh"
        answer = {
            "number": 42,
            "state": "open",
            "repository_url": f"https://api.github.com/repos/{REPOSITORY}",
        }
        fake_gh.write_text(
            "#!/bin/sh\n"
            f'echo "$@" > "{directory / "gh-arguments"}"\n'
            f"echo '{json.dumps(answer)}'\n"
        )
        fake_gh.chmod(0o755)
        event_path = directory / "event.json"
        event_path.write_text(json.dumps({"pull_request": {"body": "Fixes #42"}}))

        completed = subprocess.run(
            [sys.executable, "-m", "scripts.rules.pr_description"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_EVENT_PATH": str(event_path),
            },
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(
            (directory / "gh-arguments").read_text(),
            f"api repos/{REPOSITORY}/issues/42\n",
        )


if __name__ == "__main__":
    unittest.main()

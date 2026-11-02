"""Check that a pull request's description names its tracking issue.

Rule ``<rule_prefix>.process.pr-tracking-issue``: the first line of every pull
request's description is ``Fixes #ISSUE_NUMBER``, and ISSUE_NUMBER is an open
issue, not a pull request, in this repository. The "PR description" workflow
job runs ``python -m scripts.rules.pr_description``; it prints one error
annotation and exits 1 when the rule is broken. The issue lookup is
``scripts.rules.issues``, which the downgrade check also uses.

Environment:
  GITHUB_EVENT_PATH  the pull_request event payload, which holds the
                     description (set by the GitHub Actions runner)
  GITHUB_REPOSITORY  owner/name of this repository (set by the runner)
  GH_TOKEN           read by gh, not by this module; the workflow sets it to
                     the job's github.token, which needs only read access

How the first line is read, as in the simulation API's check:

- A line ends at a CRLF, a carriage return or a line feed, as in Markdown, so
  descriptions edited in the GitHub web interface (which uses CRLF) pass.
- Trailing spaces and tabs are ignored: they don't show, and GitHub still reads
  the closing keyword.
- Leading whitespace fails. Four leading spaces make the line a Markdown code
  block, where GitHub doesn't read closing keywords, and any indent is a sign
  the line isn't what the author meant.
- An empty or missing (``null``) description fails.
- The number is ASCII digits without a leading zero.

A pull request whose author is Dependabot (``dependabot[bot]``) passes: it
writes its own descriptions and cannot name an issue, and the weekly
``uv.lock`` updates it proposes need none.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from scripts.rules.issues import (
    IssueLookupError,
    NotAnOpenIssueError,
    require_open_issue,
)
from scripts.rules.load import load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
# The rule's id after the registry's rule_prefix.
RULE_NAME = "process.pr-tracking-issue"
FIXES_LINE = re.compile(r"Fixes #([1-9][0-9]*)")
# Markdown line endings: CRLF, a carriage return alone, or a line feed.
LINE_ENDING = re.compile(r"\r\n|\r|\n")
# How much of a rejected first line a message quotes.
SHOWN_FIRST_LINE_LENGTH = 80
ANNOTATION_TITLE = "PR description"
# The author of Dependabot's pull requests, which name no tracking issue.
DEPENDABOT = "dependabot[bot]"
HOW_TO_FIX = (
    "Edit the description so its first line is exactly Fixes #ISSUE_NUMBER, "
    "where ISSUE_NUMBER is an open issue in this repository; editing the "
    "description runs this check again"
)
RETRY_LOOKUP = (
    "Re-run this job; if it fails again, check the GH_TOKEN the workflow gives it."
)
REOPEN_ISSUE = (
    "If this pull request still fixes it, reopen it and re-run this job; "
    "otherwise open an issue for this work and edit the first line to name it"
)


class DescriptionError(Exception):
    """The description's first line does not name an issue as the rule requires."""


def rule_id(root: Path = REPO_ROOT) -> str:
    """Return the rule's id, with the registry's prefix when the registry loads."""
    registry, _ = load_registry(root)
    if registry is None or not registry.repository.rule_prefix:
        return RULE_NAME
    return registry.repository.rule_id(RULE_NAME)


def _shown(line: str) -> str:
    """Return ``line`` quoted for a message, shortened when it is long."""
    if len(line) > SHOWN_FIRST_LINE_LENGTH:
        line = line[:SHOWN_FIRST_LINE_LENGTH].rstrip() + "..."
    return f'"{line}"'


def issue_number_from_description(body: str | None) -> int:
    """Return the issue number on the description's first line.

    Raise ``DescriptionError``, with a message that says how to fix the
    description, unless the first line is ``Fixes #ISSUE_NUMBER``.
    """
    if body is None or not body.strip():
        msg = f"The description is empty. {HOW_TO_FIX}."
        raise DescriptionError(msg)

    lines = [line.rstrip(" \t") for line in LINE_ENDING.split(body)]
    first_line = lines[0]
    match = FIXES_LINE.fullmatch(first_line)
    if match:
        return int(match.group(1))

    if FIXES_LINE.fullmatch(first_line.lstrip()):
        msg = (
            f"The first line starts with whitespace: {_shown(first_line)}. "
            f"{HOW_TO_FIX}."
        )
        raise DescriptionError(msg)
    for line_number, line in enumerate(lines[1:], start=2):
        if FIXES_LINE.fullmatch(line.strip()):
            msg = (
                f"{line.strip()} is on line {line_number}, but it must be the "
                f"first line. {HOW_TO_FIX}."
            )
            raise DescriptionError(msg)
    msg = f"The first line is {_shown(first_line)}. {HOW_TO_FIX}."
    raise DescriptionError(msg)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        msg = f"{name} is required"
        raise SystemExit(msg)
    return value


def _pull_request_from_event(event_path: str) -> dict[str, object]:
    """Return the pull request in the event payload at ``event_path``."""
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull_request, dict):
        msg = (
            f"The event payload at {event_path} has no pull_request; run this "
            "check on pull_request events."
        )
        raise SystemExit(msg)
    return pull_request


def _author(pull_request: dict[str, object]) -> str | None:
    user = pull_request.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _citing_the_rule(message: str) -> str:
    """End ``message`` with the rule it enforces, as one sentence."""
    return f"{message.removesuffix('.')} (rule {rule_id()})."


def _annotation_text(message: str) -> str:
    """Escape ``message`` so the runner reads it as one annotation message."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    """Check the pull request in this run's event; return the exit status."""
    repository = _required_environment("GITHUB_REPOSITORY")
    pull_request = _pull_request_from_event(_required_environment("GITHUB_EVENT_PATH"))
    if _author(pull_request) == DEPENDABOT:
        print(
            "Dependabot opened this pull request, so its description needs no "
            "tracking issue."
        )
        return 0
    body = pull_request.get("body")
    description = body if isinstance(body, str) else None
    try:
        number = issue_number_from_description(description)
        require_open_issue(repository, number)
    except DescriptionError as error:
        message = _citing_the_rule(str(error))
    except IssueLookupError as error:
        message = f"{error} {RETRY_LOOKUP}"
    except NotAnOpenIssueError as error:
        advice = HOW_TO_FIX if error.state is None else REOPEN_ISSUE
        message = _citing_the_rule(f"{error} {advice}")
    else:
        print(
            f"The first line is Fixes #{number}, and #{number} is an open issue in "
            f"{repository}."
        )
        return 0
    print(f"::error title={ANNOTATION_TITLE}::{_annotation_text(message)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

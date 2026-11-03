"""The noqa-comments quality guard: the number of inline suppressions may not grow.

Every suppression comment Ruff applies in a tracked Python file is one entry,
``<path>: <codes>``, with the codes sorted and joined by ", ", or
``<path>: (all)`` for a blanket one:

- an inline ``noqa`` directive, with no label;
- Ruff's ``ignore[...]``, ``disable[...]`` and ``file-ignore[...]`` forms,
  labelled ``(ruff: ignore)``, ``(ruff: disable)`` and ``(ruff: file-ignore)``
  (an ``enable[...]`` ends a disabled range and is not one);
- a file-level ``ruff: noqa`` or ``flake8: noqa`` on its own line, labelled
  ``(ruff: noqa)`` or ``(flake8: noqa)``.

Codes are read as Ruff reads them: uppercase letters then digits, with or
without commas or spaces between codes, and a space is allowed before the
colon. A directive whose codes Ruff rejects, such as lowercase ones, applies
nothing and is not counted, and neither is a file-level directive after code.
Only comments count: text in a string, or a comment that names noqa without
the directive, does not. The rule's baseline file lists each entry once per
comment, so moving a comment within its file changes nothing.

The guard compares the counts per entry. It fails on a comment the baseline
does not list, and on a listed entry with fewer comments left than it lists,
so the baseline has to change with the comments. The baseline's total may
only fall against the base branch, which the rules guard checks as for every
ratcheted rule: a pull request may add a suppression only by removing another.
That comparison counts comments only, so swapping one for another at the same
total, renaming a file or adding a code to an existing comment passes, once
the baseline lists the result.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.rules.baseline import baseline_lines
from scripts.rules.load import load_registry
from scripts.rules.locked_installs import tracked_files
from scripts.rules.model import Kind, Registry, Rule, Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "noqa-comments"
# Ruff's inline directive: a hash sign and the word noqa, in any case, which
# may follow other text in the comment. What follows it is read by codes_after.
NOQA_DIRECTIVE = re.compile(r"#\s*(?i:noqa)(?P<rest>.*)")
# Ruff's bracketed forms, such as ignore[BLE001] after "ruff:".
RUFF_BRACKETED = re.compile(
    r"#\s*ruff\s*:\s*(?P<form>ignore|disable|enable|file-ignore)\s*"
    r"\[(?P<codes>[^\]]*)\]"
)
# A file-level noqa after "ruff:" or "flake8:", which Ruff applies only on a
# line of its own.
FILE_NOQA_DIRECTIVE = re.compile(
    r"#\s*(?P<tool>ruff|flake8)\s*:\s*(?i:noqa)(?P<rest>.*)"
)
RULE_CODE = re.compile(r"[A-Z]+[0-9]+")
CODE_SEPARATOR = re.compile(r"[\s,]*")
NEW_SUPPRESSION_FIX = (
    "fix the finding instead of suppressing it, or remove another suppression and "
    "list this one in {file}: the total may not grow"
)
REMOVED_SUPPRESSION_FIX = (
    "remove the extra entry from {file} and lower the rule's baseline count"
)


def codes_after(rest: str) -> list[str] | None:
    """Read the codes after a noqa directive as Ruff does.

    Return ``[]`` for a blanket directive, the codes in the order written, or
    None when Ruff applies nothing: a colon followed by no code it accepts, or
    other text joined to the word noqa.
    """
    stripped = rest.lstrip()
    if not stripped.startswith(":"):
        return [] if not rest or rest[0].isspace() or rest[0] == "#" else None
    text = stripped[1:].lstrip()
    codes = []
    position = 0
    while code := RULE_CODE.match(text, position):
        codes.append(code.group())
        position = CODE_SEPARATOR.match(text, code.end()).end()
    return codes or None


def _entry(path: str, codes: list[str], label: str = "") -> str:
    listed = ", ".join(sorted(codes)) or "(all)"
    return f"{path}: {listed}{f' ({label})' if label else ''}"


def _directive_entries(path: str, comment: str, *, own_line: bool) -> list[str]:
    """Return the entries for the suppression directives in one comment."""
    file_level = FILE_NOQA_DIRECTIVE.search(comment)
    if file_level is not None:
        codes = codes_after(file_level.group("rest"))
        if not own_line or codes is None:
            return []
        return [_entry(path, codes, f"{file_level.group('tool')}: noqa")]
    entries = [
        _entry(
            path, RULE_CODE.findall(form.group("codes")), f"ruff: {form.group('form')}"
        )
        for form in RUFF_BRACKETED.finditer(comment)
        if form.group("form") != "enable" and RULE_CODE.search(form.group("codes"))
    ]
    inline = NOQA_DIRECTIVE.search(comment)
    if inline is not None:
        codes = codes_after(inline.group("rest"))
        if codes is not None:
            entries.append(_entry(path, codes))
    return entries


def comment_entries(path: str, source: str) -> list[str]:
    """Return one entry for each suppression directive Ruff applies in ``source``."""
    entries = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        own_line = not token.line[: token.start[1]].strip()
        entries.extend(_directive_entries(path, token.string, own_line=own_line))
    return entries


def find_entries(root: Path, paths: Iterable[str]) -> Counter[str]:
    """Count the suppression entries in the Python files among ``paths``."""
    found: Counter[str] = Counter()
    for path in sorted(paths):
        if path.endswith(".py"):
            source = (root / path).read_text(encoding="utf-8")
            found.update(comment_entries(path, source))
    return found


def noqa_rule(registry: Registry) -> Rule | None:
    """Return the rule this guard enforces, if the registry has one."""
    return next(
        (
            rule
            for rule in registry.rules
            if rule.enforcer.kind is Kind.QUALITY_GUARD
            and rule.enforcer.get("name") == GUARD_NAME
        ),
        None,
    )


def baseline_entries(root: Path, rule: Rule | None) -> Counter[str]:
    """Count the entries the rule's baseline lists, skipping blanks and comments."""
    if rule is None or rule.baseline is None:
        return Counter()
    file = root / rule.baseline.file
    if not file.is_file():
        return Counter()
    return Counter(baseline_lines(file.read_text(encoding="utf-8")))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def compare(
    found: Counter[str],
    baseline: Counter[str],
    rule: Rule | None,
    baseline_file: str,
) -> list[Violation]:
    """Report each comment the baseline doesn't list, and each entry left over."""
    rule_id = rule.id if rule is not None else None
    violations = []
    for entry in sorted(found):
        violations.extend(
            Violation(
                rule_id,
                GUARD_NAME,
                f"inline suppression not in the baseline: {entry}",
                NEW_SUPPRESSION_FIX.format(file=baseline_file),
            )
            for _ in range(found[entry] - baseline[entry])
        )
    violations.extend(
        Violation(
            rule_id,
            GUARD_NAME,
            (
                f"baseline entry no longer applies: {entry} is listed "
                f"{_plural(baseline[entry], 'time')}, but the file has "
                f"{_plural(found[entry], 'such comment')}"
            ),
            REMOVED_SUPPRESSION_FIX.format(file=baseline_file),
        )
        for entry in sorted(baseline)
        if baseline[entry] > found[entry]
    )
    return violations


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Load the registry under ``root`` and return every noqa-comments violation.

    Without a rule for this guard, nothing is checked.
    """
    registry, violations = load_registry(root)
    if registry is None:
        return violations
    rule = noqa_rule(registry)
    if rule is None:
        return []
    baseline_file = rule.baseline.file if rule.baseline else "the baseline"
    found = find_entries(root, tracked_files(root))
    return compare(found, baseline_entries(root, rule), rule, baseline_file)


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]

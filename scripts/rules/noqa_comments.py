"""The noqa-comments quality guard: inline suppressions may only become fewer.

Every inline ``# noqa`` comment in a tracked Python file is one entry,
``<path>: <codes>``, with the codes sorted and joined by ", ", or
``<path>: (all)`` for a blanket ``# noqa``. Only comments count: text in a
string, or a comment that names noqa without the ``# noqa`` directive, does
not. The rule's baseline file lists each entry once per comment, so moving a
comment within its file changes nothing.

The guard compares the counts per entry. It fails on a comment the baseline
does not list, and on a listed entry with fewer comments left than it lists,
so the baseline has to shrink when a suppression goes. The baseline's count
may only fall against the base branch, which the rules guard checks as for
every ratcheted rule.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.rules.load import load_registry
from scripts.rules.locked_installs import tracked_files
from scripts.rules.model import Kind, Registry, Rule, Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "noqa-comments"
# Ruff's inline directive: a hash sign, the word noqa, then optionally a colon
# and codes separated by commas or spaces. It may follow other text in the
# comment.
NOQA_DIRECTIVE = re.compile(
    r"#\s*(?i:noqa)(?::\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?"
)
NEW_SUPPRESSION_FIX = (
    "fix the finding instead of suppressing it; {file} may only shrink"
)
REMOVED_SUPPRESSION_FIX = (
    "remove the extra entry from {file} and lower the rule's baseline count"
)


def comment_entries(path: str, source: str) -> list[str]:
    """Return one entry for each inline ``# noqa`` comment in ``source``."""
    entries = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        directive = NOQA_DIRECTIVE.search(token.string)
        if directive is None:
            continue
        codes = sorted(re.split(r"[\s,]+", directive.group("codes") or ""))
        listed = ", ".join(code for code in codes if code) or "(all)"
        entries.append(f"{path}: {listed}")
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
    return Counter(
        line.strip()
        for line in file.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


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

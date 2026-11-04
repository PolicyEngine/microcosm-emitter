"""Run repository quality guards.

Run it from the repository root as ``python -m scripts.run_quality_guards``,
so that the ``scripts`` package is importable.
"""

from __future__ import annotations

from collections.abc import Callable

from scripts.rules import (
    ci_scripts,
    file_length,
    locked_installs,
    noqa_comments,
    uv_version,
)
from scripts.rules import guard as rules_guard

Guard = tuple[str, Callable[[], list[str]]]

# Each guard needs a quality-guard rule in rules/registry.toml. The other
# checker in scripts/rules, pyright.check_coverage, is added here together with
# the rule that registers it.
GUARDS: tuple[Guard, ...] = (
    ("rules-registry", rules_guard.check),
    ("file-length", file_length.check),
    ("locked-installs", locked_installs.check),
    ("uv-version", uv_version.check),
    ("ci-scripts", ci_scripts.check),
    ("noqa-comments", noqa_comments.check),
)


def main() -> int:
    """Run every guard, print its violations, and return 1 if any guard failed.

    All guards run even after one fails, so a single run reports every problem.
    """
    failed = False
    for name, check in GUARDS:
        violations = check()
        if not violations:
            print(f"{name}: passed")
            continue

        failed = True
        print(f"{name}: failed")
        for violation in violations:
            print(f"  - {violation}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Check that every uv install pins the one uv release.

The repository's uv release is written once, as the line ``uv==X.Y.Z`` in
``.github/uv-version.txt``. The guard reads every tracked file that can
install uv (workflows, composite actions, shell scripts, Makefiles,
Dockerfiles, Compose files and Python modules that define Modal images) and
fails when an install pins no version or another one:

- an ``astral-sh/setup-uv`` step must read the release from that file, with
  ``with: version-file: .github/uv-version.txt`` and no ``version:``. Without
  either it may install the newest uv release, and a ``version:`` would be a
  second place the release is written, even when it names the same one;
- a reference to the uv container image (``ghcr.io/astral-sh/uv`` or
  ``astral/uv``), such as a Dockerfile's ``COPY --from=``, must carry the tag
  ``<version>`` or ``<version>-<variant>``;
- a Modal ``uv_sync`` or ``uv_pip_install`` call must pass
  ``uv_version="<version>"``; without it Modal copies uv from the image's
  ``latest`` tag;
- the uv install script and uv release downloads must name the version in
  their URL;
- ``pip``, ``pipx`` or ``uv tool`` installs of the ``uv`` package must pin
  ``uv==<version>``, and ``uv self update`` must name the version.

When the repository root has a ``pyproject.toml``, its ``[tool.uv]
required-version`` must be a lower bound written ``>=X.Y`` or ``>=X.Y.Z``
that the release meets: uv then refuses to run the project with a release
below the bound, while Dependabot, which runs ``uv lock`` with its own uv
release and stops when ``required-version`` excludes it, still can whenever
its release meets the bound. An exact ``==`` release there would stop
Dependabot whenever its uv differs.

Violations name the rule that lists the ``uv-version`` guard.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.rules.load import load_registry
from scripts.rules.locked_installs import file_kind, tracked_files
from scripts.rules.model import Kind, Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scripts.rules.model import Registry

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "uv-version"
# The file that holds the release, as setup-uv's version-file reads it.
VERSION_FILE = ".github/uv-version.txt"
# What a setup-uv step that reads the release from that file pins.
FROM_VERSION_FILE = f"version-file: {VERSION_FILE}"
_RELEASE_LINE = re.compile(r"uv==(?P<version>\d+\.\d+\.\d+)")
# The project at the repository root, whose [tool.uv] uv itself reads.
ROOT_PROJECT = "pyproject.toml"
_FLOOR = re.compile(r">=(?P<floor>\d+\.\d+(?:\.\d+)?)")

# The kinds of uv install, each with how to pin it.
SETUP_UV_STEP = "astral-sh/setup-uv step"
UV_IMAGE = "uv container image"
MODAL_UV = "Modal uv install"
DOWNLOAD_URL = "uv download URL"
PACKAGE_INSTALL = "install of the uv package"
SELF_UPDATE = "uv self update"

_HOW_TO_PIN = {
    SETUP_UV_STEP: f"set with: version-file: {VERSION_FILE} on the step, and no version:",
    UV_IMAGE: "use the tag ghcr.io/astral-sh/uv:{version}",
    MODAL_UV: 'pass uv_version="{version}"',
    DOWNLOAD_URL: "name {version} in the URL (https://astral.sh/uv/{version}/install.sh)",
    PACKAGE_INSTALL: "install uv=={version}",
    SELF_UPDATE: "run uv self update {version}",
}

_RELEASE = re.compile(r"\d+\.\d+\.\d+")
_KEY = re.compile(
    r"^(?P<lead>\s*)(?P<dash>-\s+)?(?P<key>[A-Za-z0-9_-]+):\s*(?P<value>.*)$"
)
_LIST_ITEM = re.compile(r"^\s*-\s+")
_SETUP_UV = re.compile(
    r"^(?P<lead>\s*)(?P<dash>-\s+)?uses:\s*[\"']?astral-sh/setup-uv@[^\s\"']+"
)
_FLOW_INPUT = r"(?:^|[{{,\s]){name}:\s*(?P<value>[^,}}\s]+)"
_COMMENT = re.compile(r"(?:^|\s)#.*$")
# The uv image on GitHub's registry or Docker Hub, with its tag if it has one.
_IMAGE = re.compile(
    r"(?:ghcr\.io/astral-sh/uv|(?<![\w.-])astral/uv)(?![\w-])"
    r"(?::(?P<version>[^\s\"'@,)]+))?"
)
_DOWNLOADS = (
    re.compile(r"astral\.sh/uv/(?:(?P<version>[^/\s\"']+)/)?install\.(?:sh|ps1)"),
    re.compile(
        r"(?:astral-sh|astral\.sh/github)/uv/releases/"
        r"(?:download/(?P<version>[^/\s\"']+)|latest)/"
    ),
)
_PACKAGE_INSTALL = re.compile(
    r"(?<![\w-])(?:pip3?|python3?\s+-m\s+pip|pipx|uv\s+tool|uv\s+pip)\s+install\s+"
    r"(?:[^\s;&|]+\s+)*?uv(?:==(?P<version>[^\s,;&|]+))?(?=[\s,);&|]|$)"
)
_SELF_UPDATE = re.compile(r"\buv\s+self\s+update\b(?:\s+(?P<version>\d[\w.]*))?")
# Modal image methods that install uv, and those whose arguments can hold
# shell commands, image references or package names.
_MODAL_UV_METHODS = ("uv_sync", "uv_pip_install")
_MODAL_TEXT_METHODS = ("run_commands", "dockerfile_commands", "from_registry")
_MODAL_PACKAGE_METHODS = ("pip_install", "uv_pip_install")
_MODAL_CALL = re.compile(
    r"\.("
    + "|".join(
        sorted({*_MODAL_UV_METHODS, *_MODAL_TEXT_METHODS, *_MODAL_PACKAGE_METHODS})
    )
    + r")\("
)
_UV_VERSION_ARGUMENT = re.compile(r"\buv_version\s*=\s*(?P<value>[^,)]+)")
_PACKAGE_ARGUMENT = re.compile(r"(?<![\w.-])uv(?:==(?P<version>[\w.+-]+))?(?![\w.=-])")
_QUOTE_PAIR_LENGTH = 2


@dataclass(frozen=True)
class UvInstall:
    """One place that installs uv, and the version it pins (None when unpinned)."""

    path: str
    line: int
    kind: str
    version: str | None
    # What the report calls the install, when it is more specific than its kind.
    label: str = ""

    @property
    def site(self) -> str:
        """Format as ``path:line: what it is``."""
        return f"{self.path}:{self.line}: {self.label or self.kind}"

    def fix(self, pinned: str) -> str:
        """Say how to make this install pin ``pinned``."""
        how = _HOW_TO_PIN[self.kind].format(version=pinned)
        return f"{how}, the release in {VERSION_FILE}"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= _QUOTE_PAIR_LENGTH and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _opens_step(line: str, key_column: int) -> bool:
    """Whether ``line`` is the ``- `` item whose keys start at ``key_column``."""
    item = _LIST_ITEM.match(line)
    return item is not None and len(item.group(0)) == key_column


def _step_lines(lines: list[str], index: int, key_column: int) -> list[str]:
    """Return the lines of the step whose ``uses:`` key is ``lines[index]``."""
    start = index
    while start > 0 and not _opens_step(lines[start], key_column):
        previous = lines[start - 1]
        if (
            previous.strip()
            and _indent(previous) < key_column
            and not _opens_step(previous, key_column)
        ):
            break
        start -= 1
    end = index + 1
    while end < len(lines) and (
        not lines[end].strip() or _indent(lines[end]) >= key_column
    ):
        end += 1
    return lines[start:end]


def _with_input(step: list[str], key_column: int, name: str) -> str | None:
    """Return the step's ``with:`` input ``name``, or None if it sets none."""
    for position, line in enumerate(step):
        entry = _KEY.match(line)
        if entry is None or entry["key"] != "with":
            continue
        if len(entry["lead"]) + len(entry["dash"] or "") != key_column:
            continue
        if entry["value"].startswith("{"):
            inline = re.search(_FLOW_INPUT.format(name=re.escape(name)), entry["value"])
            return _unquote(inline["value"]) if inline else None
        for child in step[position + 1 :]:
            if child.strip() and _indent(child) <= key_column:
                break
            argument = _KEY.match(child)
            if argument is not None and argument["key"] == name:
                return _unquote(argument["value"])
    return None


def setup_uv_steps(path: str, text: str) -> list[UvInstall]:
    """Return each ``astral-sh/setup-uv`` step in a workflow or action file."""
    lines = [_COMMENT.sub("", line).rstrip() for line in text.splitlines()]
    installs = []
    for index, line in enumerate(lines):
        match = _SETUP_UV.match(line)
        if match is None:
            continue
        key_column = len(match["lead"]) + len(match["dash"] or "")
        step = _step_lines(lines, index, key_column)
        version = _with_input(step, key_column, "version")
        if version is None and (
            _with_input(step, key_column, "version-file") == VERSION_FILE
        ):
            version = FROM_VERSION_FILE
        installs.append(UvInstall(path, index + 1, SETUP_UV_STEP, version))
    return installs


def text_installs(path: str, line_number: int, text: str) -> list[UvInstall]:
    """Return the uv images, downloads and package installs that ``text`` names."""
    unquoted = text.replace('"', " ").replace("'", " ")
    found = [
        (match.start(), UV_IMAGE, match["version"]) for match in _IMAGE.finditer(text)
    ]
    found.extend(
        (match.start(), DOWNLOAD_URL, match["version"])
        for pattern in _DOWNLOADS
        for match in pattern.finditer(text)
    )
    found.extend(
        (match.start(), PACKAGE_INSTALL, match["version"])
        for match in _PACKAGE_INSTALL.finditer(unquoted)
    )
    found.extend(
        (match.start(), SELF_UPDATE, match["version"])
        for match in _SELF_UPDATE.finditer(unquoted)
    )
    return [
        UvInstall(path, line_number, kind, version)
        for _start, kind, version in sorted(found, key=lambda item: item[0])
    ]


def _modal_calls(text: str) -> list[tuple[int, str, str]]:
    """Return each Modal image call as (line, method, argument text)."""
    calls = []
    for match in _MODAL_CALL.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if "#" in text[line_start : match.start()]:
            continue  # The call is written in a comment.
        depth = 0
        end = len(text)
        for index in range(match.end() - 1, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        line_number = text.count("\n", 0, match.start()) + 1
        calls.append((line_number, match.group(1), text[match.end() : end]))
    return calls


def modal_installs(path: str, text: str) -> list[UvInstall]:
    """Return the uv installs in a Python module's Modal image calls."""
    installs = []
    for line_number, method, arguments in _modal_calls(text):
        if method in _MODAL_UV_METHODS:
            argument = _UV_VERSION_ARGUMENT.search(arguments)
            version = _unquote(argument["value"]) if argument else None
            label = f"Modal {method} call"
            installs.append(UvInstall(path, line_number, MODAL_UV, version, label))
        if method in _MODAL_TEXT_METHODS:
            installs.extend(text_installs(path, line_number, arguments))
        if method in _MODAL_PACKAGE_METHODS:
            without_options = _UV_VERSION_ARGUMENT.sub("", arguments)
            unquoted = without_options.replace('"', " ").replace("'", " ")
            installs.extend(
                UvInstall(path, line_number, PACKAGE_INSTALL, package["version"])
                for package in _PACKAGE_ARGUMENT.finditer(unquoted)
            )
    return installs


def scan(
    root: Path,
    files: Iterable[str] | None = None,
    test_directories: Iterable[str] = (),
) -> list[UvInstall]:
    """Return every uv install in the install-capable files under ``root``.

    Python modules under ``test_directories`` are not read.
    """
    installs = []
    directories = tuple(test_directories)
    for path in sorted(tracked_files(root) if files is None else files):
        kind = file_kind(path, directories)
        if kind is None:
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        if kind == "python":
            installs.extend(modal_installs(path, text))
            continue
        if kind in {"workflow", "action"}:
            installs.extend(setup_uv_steps(path, text))
        for number, line in enumerate(text.splitlines(), start=1):
            code = _COMMENT.sub("", line)
            if code.strip():
                installs.extend(text_installs(path, number, code))
    return installs


def read_pinned_version(
    root: Path, rule_id: str | None = None
) -> tuple[str | None, list[Violation]]:
    """Return the release in the version file, or why it holds no single release.

    The violations name ``rule_id``.
    """
    source = root / VERSION_FILE
    fix = f"write one line uv==<release>, such as uv==0.13.6, in {VERSION_FILE}"
    if not source.is_file():
        message = f"{VERSION_FILE}, which holds the uv release, is missing"
        return None, [Violation(rule_id, GUARD_NAME, message, f"create it and {fix}")]
    values = [
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    match = _RELEASE_LINE.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        message = f"{VERSION_FILE} does not hold one line uv==X.Y.Z"
        return None, [Violation(rule_id, GUARD_NAME, message, fix)]
    return match["version"], []


def _release(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def required_version_problems(
    root: Path, pinned: str, rule_id: str | None = None
) -> list[Violation]:
    """Report a root required-version that is no floor, or refuses ``pinned``.

    A repository without a root pyproject.toml has nowhere to set it.
    """
    source = root / ROOT_PROJECT
    if not source.is_file():
        return []
    fix = (
        'set required-version = ">=<minor release>", such as ">=0.13", under '
        f"[tool.uv] in {ROOT_PROJECT}"
    )
    try:
        config = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return [Violation(rule_id, GUARD_NAME, f"{ROOT_PROJECT} is not TOML", fix)]
    required = config.get("tool", {}).get("uv", {}).get("required-version")
    if required is None:
        message = f"{ROOT_PROJECT} sets no [tool.uv] required-version"
        return [Violation(rule_id, GUARD_NAME, message, fix)]
    match = _FLOOR.fullmatch(str(required))
    if match is None:
        message = (
            f"{ROOT_PROJECT}'s [tool.uv] required-version is {required!r}, not a "
            "lower bound written >=X.Y"
        )
        return [Violation(rule_id, GUARD_NAME, message, fix)]
    floor = _release(match["floor"])
    if _release(pinned)[: len(floor)] < floor:
        message = (
            f"{ROOT_PROJECT}'s [tool.uv] required-version {required!r} refuses uv "
            f"{pinned}, the release in {VERSION_FILE}"
        )
        return [Violation(rule_id, GUARD_NAME, message, fix)]
    return []


def _problem(install: UvInstall, pinned: str | None) -> str | None:
    """Say why ``install`` doesn't pin ``pinned``, or return None if it does."""
    version = install.version
    if version is None:
        return f"{install.site} does not pin a uv release"
    if install.kind == SETUP_UV_STEP and version == FROM_VERSION_FILE:
        return None
    if not _RELEASE.match(version):
        return f"{install.site} does not pin a uv release (it names {version})"
    if install.kind == SETUP_UV_STEP:
        return (
            f"{install.site} writes uv {version} itself instead of reading "
            f"{VERSION_FILE}"
        )
    # An image tag may add a variant to the release, as in 0.13.6-python3.13-bookworm.
    image_variant = install.kind == UV_IMAGE and version.startswith(f"{pinned}-")
    if pinned is None or version == pinned or image_variant:
        return None
    return f"{install.site} pins uv {version}, not {pinned}"


def compare(
    installs: list[UvInstall], pinned: str | None, rule_id: str | None = None
) -> list[Violation]:
    """Report each install that pins no release, or not the pinned one.

    The violations name ``rule_id``.
    """
    violations = []
    for install in installs:
        problem = _problem(install, pinned)
        if problem is not None:
            fix = install.fix(pinned or "<version>")
            violations.append(Violation(rule_id, GUARD_NAME, problem, fix))
    return violations


def guard_rule_id(registry: Registry | None) -> str | None:
    """Return the id of the registry's rule that lists this guard, if any."""
    if registry is None:
        return None
    return next(
        (
            rule.id
            for rule in registry.rules
            if rule.enforcer.kind is Kind.QUALITY_GUARD
            and rule.enforcer.get("name") == GUARD_NAME
        ),
        None,
    )


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Return every uv install under ``root`` that doesn't pin the one release."""
    registry, _ = load_registry(root)
    rule_id = guard_rule_id(registry)
    test_directories = registry.repository.test_directories if registry else ()
    pinned, violations = read_pinned_version(root, rule_id)
    return [
        *violations,
        *compare(scan(root, test_directories=test_directories), pinned, rule_id),
        *(required_version_problems(root, pinned, rule_id) if pinned else []),
    ]


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]

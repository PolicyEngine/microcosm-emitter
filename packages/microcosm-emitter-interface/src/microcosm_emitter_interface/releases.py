"""``releases.json``: the only record of which build of each release is current.

Each dataset repository publishes builds side by side, never changed after
publishing::

    releases.json
    releases/<release>/<build>/release_manifest.json
    releases/<release>/<build>/...

and one ``releases.json`` at its root names, for each release, its role, its
current build and every published build. This module holds the record's
shape, the three changes the release writer makes (publish a build, make a
build current, withdraw a build), and the checks every write must pass. The
emitter's release writer runs these checks before each commit; Microcosm and
a scheduled repository check can run the same code.

Not settled yet, and kept out of these rules on purpose: whether a release
such as the dense one has a current build (``current`` may be ``null``), who
may promote or withdraw (the writer's caller decides), how readers show
withdrawn builds, and whether the US layout moves to the same scheme. Nothing
here depends on a country.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from microcosm_emitter_interface.identifiers import role_identifier, safe_identifier

RELEASES_RECORD_PATH: Final = "releases.json"
RELEASES_SCHEMA_VERSION: Final = 1
RELEASES_FOLDER: Final = "releases"
RELEASE_MANIFEST_NAME: Final = "release_manifest.json"
#: Pointer files the record replaces (``latest.json``, ``latest-national.json``,
#: ``latest-evidence.json``, ...). No write may create one, and ``current``
#: may not change while any still exists.
POINTER_FILE_PATTERN: Final = "latest*.json"
#: The largest ``releases.json`` accepted. A record lists a few hundred bytes
#: per build, so this leaves room for thousands of builds.
MAX_RECORD_BYTES: Final = 4 * 1024 * 1024
#: What ``json.loads`` raises for bytes that are not a usable JSON document:
#: invalid JSON or text, and nesting too deep to decode (``RecursionError``).
_UNREADABLE_JSON_ERRORS: Final = (ValueError, UnicodeDecodeError, RecursionError)

_RECORD_KEYS: Final = {"schema_version", "default", "releases"}
_RELEASE_KEYS: Final = {"role", "current", "builds"}
_BUILD_KEYS: Final = {"published_at", "withdrawn"}


@dataclass(frozen=True)
class Violation:
    """One broken rule: a fixed ``code`` and a message without secrets."""

    code: str
    message: str


class ReleaseRecordError(ValueError):
    """A record or a change that breaks the publication rules."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(violation.message for violation in violations))


def _refuse(code: str, message: str) -> ReleaseRecordError:
    return ReleaseRecordError([Violation(code, message)])


@dataclass(frozen=True)
class BuildEntry:
    published_at: str
    withdrawn: bool = False


@dataclass(frozen=True)
class ReleaseEntry:
    role: str
    current: str | None
    builds: Mapping[str, BuildEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class ReleasesRecord:
    """A parsed, structurally valid ``releases.json``."""

    default: str | None
    releases: Mapping[str, ReleaseEntry]

    @classmethod
    def empty(cls) -> ReleasesRecord:
        return cls(default=None, releases={})

    @classmethod
    def parse(cls, content: bytes) -> ReleasesRecord:
        if len(content) > MAX_RECORD_BYTES:
            error_message = "INVALID_RECORD"
            raise _refuse(
                error_message,
                f"releases.json is larger than {MAX_RECORD_BYTES} bytes.",
            )
        try:
            document = json.loads(content)
        except _UNREADABLE_JSON_ERRORS:
            error_message = "INVALID_RECORD"
            raise _refuse(
                error_message, "releases.json is not a readable JSON document."
            ) from None
        record = cls._from_document(document)
        violations = check_record(record)
        if violations:
            raise ReleaseRecordError(violations)
        return record

    @classmethod
    def _from_document(cls, document: Any) -> ReleasesRecord:
        def invalid(message: str) -> ReleaseRecordError:
            return _refuse("INVALID_RECORD", message)

        if not isinstance(document, dict) or set(document) != _RECORD_KEYS:
            error_message = f"releases.json must have exactly {sorted(_RECORD_KEYS)}."
            raise invalid(error_message)
        version = document["schema_version"]
        # ``True`` and ``1.0`` equal 1 in Python; only the integer is the version.
        if type(version) is not int or version != RELEASES_SCHEMA_VERSION:
            error_message = (
                f"releases.json schema_version must be {RELEASES_SCHEMA_VERSION}."
            )
            raise invalid(error_message)
        releases_document = document["releases"]
        if not isinstance(releases_document, dict):
            error_message = "releases must be an object."
            raise invalid(error_message)
        releases = {}
        for release, entry in releases_document.items():
            try:
                safe_identifier(release, label="release")
            except ValueError:
                error_message = "A release name is not a safe identifier."
                raise invalid(error_message) from None
            if not isinstance(entry, dict) or set(entry) != _RELEASE_KEYS:
                error_message = f"Release {release} must have {sorted(_RELEASE_KEYS)}."
                raise invalid(error_message)
            try:
                role_identifier(entry["role"], label="release role")
            except ValueError:
                error_message = f"Release {release} has an invalid role."
                raise invalid(error_message) from None
            if entry["current"] is not None and not isinstance(entry["current"], str):
                error_message = f"Release {release} current must be a build or null."
                raise invalid(error_message)
            if not isinstance(entry["builds"], dict):
                error_message = f"Release {release} builds must be an object."
                raise invalid(error_message)
            builds = {}
            for build, build_entry in entry["builds"].items():
                try:
                    safe_identifier(build, label="build")
                except ValueError:
                    error_message = "A build name is not a safe identifier."
                    raise invalid(error_message) from None
                if (
                    not isinstance(build_entry, dict)
                    or set(build_entry) != _BUILD_KEYS
                    or not isinstance(build_entry["published_at"], str)
                    or not build_entry["published_at"]
                    or not isinstance(build_entry["withdrawn"], bool)
                ):
                    error_message = f"Build {build} of {release} is malformed."
                    raise invalid(error_message)
                builds[build] = BuildEntry(
                    build_entry["published_at"], build_entry["withdrawn"]
                )
            releases[release] = ReleaseEntry(entry["role"], entry["current"], builds)
        default = document["default"]
        if default is not None and not isinstance(default, str):
            error_message = "default must be a release name or null."
            raise invalid(error_message)
        return cls(default=default, releases=releases)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": RELEASES_SCHEMA_VERSION,
            "default": self.default,
            "releases": {
                release: {
                    "role": entry.role,
                    "current": entry.current,
                    "builds": {
                        build: {
                            "published_at": build_entry.published_at,
                            "withdrawn": build_entry.withdrawn,
                        }
                        for build, build_entry in sorted(entry.builds.items())
                    },
                }
                for release, entry in sorted(self.releases.items())
            },
        }

    def serialize(self) -> bytes:
        return (json.dumps(self.to_document(), indent=2) + "\n").encode()


def build_folder(release: str, build: str) -> str:
    return f"{RELEASES_FOLDER}/{release}/{build}"


def is_pointer_file(path: str) -> bool:
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], POINTER_FILE_PATTERN)


# Changes -----------------------------------------------------------------


def add_build(
    record: ReleasesRecord,
    *,
    release: str,
    build: str,
    release_role: str,
    published_at: str,
    make_current: bool,
) -> ReleasesRecord:
    """List a newly published build; optionally make it the release's current."""
    safe_identifier(release, label="release")
    safe_identifier(build, label="build")
    role_identifier(release_role, label="release role")
    existing = record.releases.get(release)
    if existing is not None and existing.role != release_role:
        error_message = "RELEASE_ROLE_DIFFERS"
        raise _refuse(
            error_message,
            f"Release {release} has role {existing.role}, not {release_role}.",
        )
    entry = existing or ReleaseEntry(role=release_role, current=None, builds={})
    if build in entry.builds:
        error_message = "BUILD_ALREADY_LISTED"
        raise _refuse(error_message, f"Build {build} of {release} is listed.")
    builds = {**entry.builds, build: BuildEntry(published_at, False)}
    updated = replace(
        entry, builds=builds, current=build if make_current else entry.current
    )
    return replace(record, releases={**record.releases, release: updated})


def make_current(record: ReleasesRecord, *, release: str, build: str) -> ReleasesRecord:
    """Promote a listed, non-withdrawn build."""
    entry = record.releases.get(release)
    if entry is None or build not in entry.builds:
        error_message = "BUILD_NOT_LISTED"
        raise _refuse(error_message, f"Build {build} of {release} is not listed.")
    if entry.builds[build].withdrawn:
        error_message = "CURRENT_WITHDRAWN"
        raise _refuse(error_message, f"Build {build} of {release} has been withdrawn.")
    return replace(
        record,
        releases={**record.releases, release: replace(entry, current=build)},
    )


def withdraw_build(
    record: ReleasesRecord, *, release: str, build: str
) -> ReleasesRecord:
    """Mark a listed build withdrawn; its folder and files stay.

    The current build cannot be withdrawn: make another build current first.
    """
    entry = record.releases.get(release)
    if entry is None or build not in entry.builds:
        error_message = "BUILD_NOT_LISTED"
        raise _refuse(error_message, f"Build {build} of {release} is not listed.")
    if entry.current == build:
        error_message = "WITHDRAWING_CURRENT_BUILD"
        raise _refuse(
            error_message,
            f"Build {build} is the current build of {release}.",
        )
    builds = {**entry.builds, build: replace(entry.builds[build], withdrawn=True)}
    return replace(
        record, releases={**record.releases, release: replace(entry, builds=builds)}
    )


# Checks ------------------------------------------------------------------


def check_record(record: ReleasesRecord) -> list[Violation]:
    """``current`` names a listed, non-withdrawn build; ``default`` a release."""
    violations = []
    if record.default is not None and record.default not in record.releases:
        violations.append(
            Violation("DEFAULT_NOT_LISTED", f"Default {record.default} is not listed.")
        )
    for release, entry in record.releases.items():
        if entry.current is None:
            continue
        build_entry = entry.builds.get(entry.current)
        if build_entry is None:
            violations.append(
                Violation(
                    "CURRENT_NOT_LISTED",
                    f"Current build {entry.current} of {release} is not listed.",
                )
            )
        elif build_entry.withdrawn:
            violations.append(
                Violation(
                    "CURRENT_WITHDRAWN",
                    f"Current build {entry.current} of {release} is withdrawn.",
                )
            )
    return violations


def check_change(
    before: ReleasesRecord | None, after: ReleasesRecord
) -> list[Violation]:
    """Existing build entries are unchanged except for ``withdrawn``, and the
    new record stays within the size every reader accepts."""
    violations = check_record(after)
    if len(after.serialize()) > MAX_RECORD_BYTES:
        violations.append(
            Violation(
                "RECORD_TOO_LARGE",
                f"releases.json would be larger than {MAX_RECORD_BYTES} bytes.",
            )
        )
    if before is None:
        return violations
    for release, entry in before.releases.items():
        updated = after.releases.get(release)
        if updated is None:
            violations.append(
                Violation("RELEASE_REMOVED", f"Release {release} was removed.")
            )
            continue
        for build, build_entry in entry.builds.items():
            updated_build = updated.builds.get(build)
            if updated_build is None:
                violations.append(
                    Violation(
                        "BUILD_ENTRY_REMOVED",
                        f"Build {build} of {release} was removed.",
                    )
                )
            elif updated_build.published_at != build_entry.published_at:
                violations.append(
                    Violation(
                        "BUILD_ENTRY_CHANGED",
                        f"Build {build} of {release} changed beyond withdrawn.",
                    )
                )
    return violations


def check_repository(
    record: ReleasesRecord,
    files: Collection[str],
    read_file: Callable[[str], bytes],
) -> list[Violation]:
    """Every listed build has its folder and a manifest naming it.

    ``files`` lists the repository's paths; ``read_file`` returns a path's
    bytes, so the caller decides where they come from (a pinned revision, or
    the local files about to be committed).
    """
    violations = []
    paths = set(files)
    for release, entry in sorted(record.releases.items()):
        for build in sorted(entry.builds):
            folder = build_folder(release, build)
            if not any(path.startswith(folder + "/") for path in paths):
                violations.append(
                    Violation("BUILD_FOLDER_MISSING", f"{folder}/ holds no files.")
                )
                continue
            manifest_path = f"{folder}/{RELEASE_MANIFEST_NAME}"
            if manifest_path not in paths:
                violations.append(
                    Violation("MANIFEST_MISSING", f"{manifest_path} is missing.")
                )
                continue
            content = read_file(manifest_path)
            try:
                manifest = json.loads(content)
            except _UNREADABLE_JSON_ERRORS:
                manifest = None
            if (
                not isinstance(manifest, dict)
                or manifest.get("release") != release
                or manifest.get("build") != build
            ):
                violations.append(
                    Violation(
                        "MANIFEST_MISMATCH",
                        f"{manifest_path} does not name release {release} "
                        f"and build {build}.",
                    )
                )
    return violations


def _build_folder_of(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == RELEASES_FOLDER:
        return "/".join(parts[:3])
    return None


def _current_builds(record: ReleasesRecord | None) -> dict[str, str | None]:
    if record is None:
        return {}
    return {release: entry.current for release, entry in record.releases.items()}


def check_commit(
    *,
    before_files: Collection[str],
    added_paths: Collection[str],
    deleted_paths: Collection[str],
    before: ReleasesRecord | None,
    after: ReleasesRecord,
) -> list[Violation]:
    """Rules on what one commit writes, given the repository it is built on.

    - No file inside an existing build folder is added, changed or removed,
      whether or not ``releases.json`` lists that build.
    - No write creates a ``latest*.json`` pointer file.
    - ``current`` does not change while any ``latest*.json`` file exists:
      until the old pointer files are deleted, they and the record could
      disagree.
    """
    violations = []
    existing_folders = {
        folder
        for folder in (_build_folder_of(path) for path in before_files)
        if folder is not None
    }
    if before is not None:
        existing_folders |= {
            build_folder(release, build)
            for release, entry in before.releases.items()
            for build in entry.builds
        }
    for path in sorted({*added_paths, *deleted_paths}):
        if _build_folder_of(path) in existing_folders:
            violations.append(
                Violation(
                    "EXISTING_BUILD_FOLDER_CHANGED",
                    f"{path} is inside an existing build folder.",
                )
            )
    for path in sorted(added_paths):
        if is_pointer_file(path):
            violations.append(
                Violation("POINTER_FILE_WRITE", f"{path} is a pointer file.")
            )
    before_current = _current_builds(before)
    changed_current = any(
        before_current.get(release) != current
        for release, current in _current_builds(after).items()
    ) or any(release not in after.releases for release in before_current)
    pointer_files = sorted(path for path in before_files if is_pointer_file(path))
    if changed_current and pointer_files:
        violations.append(
            Violation(
                "POINTER_FILES_PRESENT",
                "current cannot change while pointer files exist: "
                + ", ".join(pointer_files),
            )
        )
    return violations

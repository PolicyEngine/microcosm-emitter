"""Which collector submissions a hand-over produces.

A staged bundle or release publication gives one submission per kind whose
files it names: this only selects files by the roles the hand-over names and
maps them to the collector contract's roles; it never reads or interprets
file contents. A promotion or withdrawal gives one ``releases-record``
submission of the ``releases.json`` its commit wrote, naming the build that
is current after the change.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from microcosm_emitter_interface.bundles import CALIBRATION_DIAGNOSTICS_ROLE
from microcosm_emitter_interface.releases import RELEASES_RECORD_PATH, build_folder
from microcosm_emitter_interface.submissions import (
    EXECUTION_OVERLAY_ROLE,
    ORRERY_EVIDENCE_ROLE,
    ORRERY_SCHEMA_ROLE,
    ORRERY_UPSTREAM_ROLE,
)

SUBMISSION_CONTRACT_VERSION: Final = 1
#: The longest ``provenance.huggingface.folder`` the collector accepts.
MAX_FOLDER_CHARACTERS: Final = 512
CALIBRATION_DIAGNOSTICS_KIND: Final = "calibration-diagnostics"
ORRERY_INPUTS_KIND: Final = "orrery-inputs"
RELEASES_RECORD_KIND: Final = "releases-record"
#: The collector's role for the one file of a ``releases-record`` submission.
RELEASES_RECORD_ROLE: Final = "releases_record"
#: The command of a release publication.
PUBLISH_RELEASE_COMMAND: Final = "publish_release"
#: The release changes that change only ``releases.json``. Every hand-over
#: with collector provenance that is neither one of these nor a publication
#: is a staged bundle.
RECORD_CHANGE_COMMANDS: Final = frozenset({"promote_build", "withdraw_build"})


@dataclass(frozen=True)
class SubmissionKind:
    """Hand-over roles a kind takes, mapped to the collector's role names."""

    name: str
    roles: Mapping[str, str]
    required_roles: frozenset[str]


SUBMISSION_KINDS: Final = (
    SubmissionKind(
        name=CALIBRATION_DIAGNOSTICS_KIND,
        roles={
            CALIBRATION_DIAGNOSTICS_ROLE: "calibration_diagnostics",
            "build_manifest": "build_manifest",
            "release_manifest": "release_manifest",
            "demographics": "demographics",
        },
        required_roles=frozenset({CALIBRATION_DIAGNOSTICS_ROLE}),
    ),
    SubmissionKind(
        name=ORRERY_INPUTS_KIND,
        roles={
            ORRERY_SCHEMA_ROLE: "schema",
            EXECUTION_OVERLAY_ROLE: "execution_overlay",
            ORRERY_UPSTREAM_ROLE: "upstream",
            ORRERY_EVIDENCE_ROLE: "evidence",
        },
        required_roles=frozenset({ORRERY_SCHEMA_ROLE, EXECUTION_OVERLAY_ROLE}),
    ),
)


@dataclass(frozen=True)
class PlannedSubmission:
    """One ``POST /v1/submissions`` body and where each of its files is.

    ``copied`` holds the bytes of files copied when the bundle or publication
    was handed over; they are submitted as they were then, since the build
    may have rewritten them since. ``local_paths`` gives the path of each file
    submitted from the build's disk. A ``releases-record`` submission's one
    file, the ``releases.json`` its change wrote, is only in ``copied``.
    """

    kind: str
    body: dict[str, Any]
    local_paths: dict[str, str]
    copied: dict[str, bytes] = field(default_factory=dict)


def _kinds_named(request: Mapping[str, Any]) -> list[SubmissionKind]:
    roles = {entry["role"] for entry in request.get("files", [])}
    return [kind for kind in SUBMISSION_KINDS if kind.required_roles <= roles]


def is_publication(request: Mapping[str, Any]) -> bool:
    """Whether a hand-over is a release publication rather than a staged bundle."""
    return request.get("command") == PUBLISH_RELEASE_COMMAND


def is_record_change(request: Mapping[str, Any]) -> bool:
    """Whether a hand-over is a promotion or withdrawal: a ``releases.json`` change."""
    return request.get("command") in RECORD_CHANGE_COMMANDS


def wants_releases_record(request: Mapping[str, Any]) -> bool:
    """Whether a hand-over is a promotion or withdrawal that describes its build.

    Such a hand-over is submitted as a ``releases-record`` once it lands, so
    the ``releases.json`` its commit wrote is kept for its collector row.
    """
    return is_record_change(request) and isinstance(request.get("collector"), Mapping)


def submission_folder(request: Mapping[str, Any]) -> str:
    """Return the Hugging Face folder that holds the hand-over's files.

    A release publication's is its build folder, ``releases/<release>/<build>``;
    a bundle's is ``<prefix>/<run_id>``.
    """
    if is_publication(request):
        return build_folder(request["release"], request["build"])
    return f"{request['prefix']}/{request['run_id']}"


def wants_collector_submission(request: Mapping[str, Any]) -> bool:
    """Whether a hand-over describes its build and gives the collector a submission.

    A bundle or publication must name files the collector takes; a described
    promotion or withdrawal always gives a ``releases-record`` submission
    unless it leaves no current build, which is known only once it lands.
    """
    if wants_releases_record(request):
        return True
    return isinstance(request.get("collector"), Mapping) and bool(_kinds_named(request))


def submitted_file_names(request: Mapping[str, Any]) -> frozenset[str]:
    """Return the names of the hand-over's files that its submissions send."""
    kinds = _kinds_named(request)
    return frozenset(
        entry["name"]
        for entry in request.get("files", [])
        if any(entry["role"] in kind.roles for kind in kinds)
    )


def planned_submissions(
    request: Mapping[str, Any],
    revision: str,
    copied: Mapping[str, bytes] | None = None,
) -> list[PlannedSubmission]:
    """Plan the submissions for a hand-over committed to Hugging Face at ``revision``.

    ``copied`` are the bytes copied at hand-over, by file name. There are no
    submissions when the request has no ``collector`` mapping. A release
    publication names no telemetry run, so its build ID is the run ID, as in
    the collector contract's example (for Microcosm the two are the same).
    """
    copied = {} if copied is None else copied

    collector = request.get("collector")
    if not isinstance(collector, Mapping):
        return []
    run_id = collector["build_id"] if is_publication(request) else request["run_id"]
    provenance = {
        "run_id": run_id,
        "build_id": collector["build_id"],
        "country_code": collector["country_code"],
        "release": collector.get("release"),
        "huggingface": {
            "repo": request["repository"],
            "commit": revision,
            "folder": submission_folder(request),
        },
    }
    planned = []
    for kind in _kinds_named(request):
        entries = sorted(
            (entry for entry in request["files"] if entry["role"] in kind.roles),
            key=lambda entry: entry["name"],
        )
        planned.append(
            PlannedSubmission(
                kind=kind.name,
                body={
                    "contract_version": SUBMISSION_CONTRACT_VERSION,
                    "kind": kind.name,
                    "licensed_data": collector["licensed_data"],
                    "provenance": provenance,
                    "files": [
                        {
                            "name": entry["name"],
                            "role": kind.roles[entry["role"]],
                            "size": entry["bytes"],
                            "sha256": entry["sha256"],
                        }
                        for entry in entries
                    ],
                },
                local_paths={entry["name"]: entry["local_path"] for entry in entries},
                copied={
                    entry["name"]: copied[entry["name"]]
                    for entry in entries
                    if entry["name"] in copied
                },
            )
        )
    return planned


def planned_releases_record(
    request: Mapping[str, Any], commit: str, current: str, record: bytes
) -> PlannedSubmission:
    """Plan the ``releases-record`` submission of a promotion or withdrawal.

    ``commit`` is the commit the change landed with, ``current`` the release's
    current build after it, and ``record`` the ``releases.json`` at that
    commit. The collector requires the folder ``releases/<release>/<build_id>``
    of the build made current, so the submission names ``current``, which
    for a withdrawal is not the withdrawn build. A promotion or withdrawal
    names no telemetry run, so the build ID is the run ID, as for
    publications. The country and ``licensed_data`` are the provenance's.
    """
    collector = request["collector"]
    release = request["release"]
    return PlannedSubmission(
        kind=RELEASES_RECORD_KIND,
        body={
            "contract_version": SUBMISSION_CONTRACT_VERSION,
            "kind": RELEASES_RECORD_KIND,
            "licensed_data": collector["licensed_data"],
            "provenance": {
                "run_id": current,
                "build_id": current,
                "country_code": collector["country_code"],
                "release": release,
                "huggingface": {
                    "repo": request["repository"],
                    "commit": commit,
                    "folder": build_folder(release, current),
                },
            },
            "files": [
                {
                    "name": RELEASES_RECORD_PATH,
                    "role": RELEASES_RECORD_ROLE,
                    "size": len(record),
                    "sha256": hashlib.sha256(record).hexdigest(),
                }
            ],
        },
        local_paths={},
        copied={RELEASES_RECORD_PATH: record},
    )

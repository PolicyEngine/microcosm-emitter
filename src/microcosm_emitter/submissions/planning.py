"""Which collector submissions a staged bundle or release publication produces.

This only selects files by the roles the hand-over names and maps them to the
collector contract's roles; it never reads or interprets file contents.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from microcosm_emitter_interface.bundles import CALIBRATION_DIAGNOSTICS_ROLE
from microcosm_emitter_interface.releases import build_folder
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
#: The command of a release publication; every other hand-over with
#: collector provenance is a staged bundle.
PUBLISH_RELEASE_COMMAND: Final = "publish_release"


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
    may have rewritten them since.
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


def submission_folder(request: Mapping[str, Any]) -> str:
    """Return the Hugging Face folder that holds the hand-over's files.

    A release publication's is its build folder, ``releases/<release>/<build>``;
    a bundle's is ``<prefix>/<run_id>``.
    """
    if is_publication(request):
        return build_folder(request["release"], request["build"])
    return f"{request['prefix']}/{request['run_id']}"


def wants_collector_submission(request: Mapping[str, Any]) -> bool:
    """Whether a hand-over describes its build and names files the collector takes."""
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

"""Typed staging hand-overs: run files under ``<prefix>/<run_id>/``.

The build writes and validates its staging documents locally, as before, and
hands each file over by path. The emitter copies the bytes into its queue
before answering, then writes every pending path of a repository in one
Hugging Face commit per cycle. Uploads stay best-effort: no call here raises
because the emitter or the Hub is unavailable.

Constructing a request that breaks the path rules raises ``ValueError``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from microcosm_emitter_interface.identifiers import (
    relative_file_path,
    repository_identifier,
    repository_prefix,
    safe_identifier,
)
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.serialization import wire_value
from microcosm_emitter_interface.spool import default_handover_database_path

DEFAULT_RUN_PREFIX = "runs"
DEFAULT_COMMIT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class HuggingFaceSettings:
    """Configuration for the emitter's Hugging Face module.

    The module keeps its queue in its own SQLite file, the hand-over database.
    ``handover_database_path`` names it; by default it sits next to the spool
    database (``events.sqlite3`` → ``events.handovers.sqlite3``). It can never
    be the spool database itself, which released emitters share and which
    must stay at a migration revision they accept.
    """

    handover_database_path: Path | None = None

    def configuration(self, spool_path: Path) -> dict[str, Any]:
        handover_database = (
            Path(self.handover_database_path)
            if self.handover_database_path is not None
            else default_handover_database_path(spool_path)
        )
        if handover_database.resolve() == Path(spool_path).resolve():
            error_message = (
                "The hand-over database must be a different file from the "
                "spool database."
            )
            raise ValueError(error_message)
        return {"handover_database_path": str(handover_database)}


@dataclass(frozen=True)
class RunFile:
    """One local file, written at ``<prefix>/<run_id>/<relative_path>``."""

    relative_path: str
    local_path: Path

    def __post_init__(self) -> None:
        relative_file_path(self.relative_path, label="relative_path")


@dataclass(frozen=True)
class StageRunFiles:
    """Hand over a staging run's current files.

    - ``files`` land under ``<prefix>/<run_id>/``, as Microcosm's staging
      writers lay them out (``runs/<run_id>/progress.json``, ...).
    - ``latest_pointer`` is staging contract version 1's pointer file, written
      to ``latest_staging.json`` at the repository root.
    - ``run_index_entry`` is version 1's summary of this run; the emitter
      merges it into the repository's ``runs.json`` when it commits.
    - An unforced hand-over is committed one ``commit_interval_seconds`` after
      this machine's last commit to the repository; ``flush`` asks for the
      next cycle (final states and artifacts). Either way a cycle makes one
      commit per repository, carrying the newest bytes of every pending path,
      and two commits from this machine to one repository are at least 10
      seconds apart.
    """

    repository: str
    run_id: str
    files: Sequence[RunFile] = ()
    prefix: str = DEFAULT_RUN_PREFIX
    latest_pointer: Path | None = None
    run_index_entry: Mapping[str, Any] | None = None
    flush: bool = False
    commit_interval_seconds: float = DEFAULT_COMMIT_INTERVAL_SECONDS
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        repository_identifier(self.repository)
        safe_identifier(self.run_id, label="run_id")
        repository_prefix(self.prefix)
        object.__setattr__(self, "files", tuple(self.files))
        if any(not isinstance(item, RunFile) for item in self.files):
            error_message = "files must be RunFile values."
            raise ValueError(error_message)
        if not isinstance(self.flush, bool):
            error_message = "flush must be a boolean."
            # The published constructors raise ValueError for every invalid value.
            raise ValueError(error_message)  # noqa: TRY004
        interval = self.commit_interval_seconds
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval < 0
        ):
            error_message = "commit_interval_seconds must be finite and >= 0."
            raise ValueError(error_message)
        object.__setattr__(self, "commit_interval_seconds", float(interval))
        if self.run_index_entry is not None and (
            not isinstance(self.run_index_entry, Mapping)
            or self.run_index_entry.get("run_id") != self.run_id
        ):
            error_message = "run_index_entry must describe this run."
            raise ValueError(error_message)

    def message(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "command": "stage_run_files",
            "repository": self.repository,
            "run_id": self.run_id,
            "prefix": self.prefix,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "local_path": str(Path(item.local_path).resolve()),
                }
                for item in self.files
            ],
            "latest_pointer": (
                None
                if self.latest_pointer is None
                else str(Path(self.latest_pointer).resolve())
            ),
            "run_index_entry": (
                None
                if self.run_index_entry is None
                else wire_value(dict(self.run_index_entry))
            ),
            "flush": self.flush,
            "commit_interval_seconds": self.commit_interval_seconds,
        }


def run_reference_message(command: str, repository: str, run_id: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "command": command,
        "repository": repository_identifier(repository),
        "run_id": safe_identifier(run_id, label="run_id"),
    }


class RunFilesReceipt(TypedDict):
    """What the emitter holds for one run, and what reached the Hub.

    ``status`` is ``recorded`` once the emitter answered, ``refused`` for a
    request it will not queue (``refusal_code`` says why), and
    ``unavailable`` when no emitter answered. File counts include each
    file of every attempted commit.
    """

    status: Literal["recorded", "refused", "unavailable"]
    repository: str
    run_id: str
    pending_files: int
    files_attempted: int
    files_committed: int
    commit_attempts: int
    commits: int
    last_error_code: str | None
    last_commit: str | None
    refusal_code: NotRequired[str]


class ReadBackResult(TypedDict):
    """Whether the Hub holds exactly the bytes the emitter committed.

    ``passed`` needs at least one committed file. A run with none (its
    hand-overs were refused, or another machine's emitter staged it) reads
    back as ``failed`` with ``error_code`` ``NO_COMMITTED_FILES``.
    """

    status: Literal["passed", "failed", "pending", "refused", "unavailable"]
    files: int
    mismatched_paths: list[str]
    error_code: str | None


def unavailable_run_files_receipt(repository: str, run_id: str) -> RunFilesReceipt:
    return {
        "status": "unavailable",
        "repository": repository,
        "run_id": run_id,
        "pending_files": 0,
        "files_attempted": 0,
        "files_committed": 0,
        "commit_attempts": 0,
        "commits": 0,
        "last_error_code": None,
        "last_commit": None,
    }


def unavailable_read_back() -> ReadBackResult:
    return {
        "status": "unavailable",
        "files": 0,
        "mismatched_paths": [],
        "error_code": None,
    }

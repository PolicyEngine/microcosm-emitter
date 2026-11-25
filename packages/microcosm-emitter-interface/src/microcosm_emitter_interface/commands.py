"""Run the installed emitter's ``version``, ``status`` and ``telemetry-token``.

The types and exit codes are in place; the calls do not run their commands
yet, so each returns a ``could_not_run`` failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Final, Literal

from microcosm_emitter_interface.protocol import STARTUP_TIMEOUT_SECONDS

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

type CommandName = Literal["version", "status", "telemetry-token"]

type CommandFailureReason = Literal[
    "invalid_arguments",
    "command_not_found",
    "could_not_run",
    "timed_out",
    "usage_error",
    "missing_credential",
    "rejected_credential",
    "collector_unavailable",
    "lifetime_unsupported",
    "unexpected_failure",
    "no_event_queue_file",
    "unusable_event_queue_file",
    "exited",
    "unusable_output",
]

COMMAND_NAMES: Final[tuple[CommandName, ...]] = ("version", "status", "telemetry-token")

EXIT_USAGE_ERROR: Final = 2
EXIT_MISSING_CREDENTIAL: Final = 3
EXIT_REJECTED_CREDENTIAL: Final = 4
EXIT_COLLECTOR_UNAVAILABLE: Final = 5
EXIT_LIFETIME_UNSUPPORTED: Final = 6
EXIT_UNEXPECTED_FAILURE: Final = 7
EXIT_NO_EVENT_QUEUE_FILE: Final = 8
EXIT_UNUSABLE_EVENT_QUEUE_FILE: Final = 9

VERSION_TIMEOUT_SECONDS: Final = STARTUP_TIMEOUT_SECONDS
STATUS_TIMEOUT_SECONDS: Final = 60.0
TELEMETRY_TOKEN_TIMEOUT_SECONDS: Final = 120.0
MAX_MESSAGE_CHARS: Final = 300

COMMAND_FIXTURE_DIRECTORY: Final = "commands"


@dataclass(frozen=True)
class CommandFailure:
    """Why a command call has no answer."""

    command: CommandName
    reason: CommandFailureReason
    exit_code: int | None = None
    message: str | None = None
    error_type: str | None = None
    output_problem: str | None = None


@dataclass(frozen=True)
class EmitterVersion:
    """What ``microcosm-emitter version`` reports."""

    emitter_version: str | None
    protocol_versions: tuple[int, ...] | None
    collector_origin: str | None


@dataclass(frozen=True)
class EventQueueRun:
    """One run in an emitter's event queue file, as ``status`` prints it."""

    run_id: str
    producer_id: str
    upload_state: str
    reason: str | None
    undelivered_events: int


@dataclass(frozen=True)
class EmitterStatus:
    """Every run ``microcosm-emitter status`` printed, in its order."""

    runs: tuple[EventQueueRun, ...]


@dataclass(frozen=True)
class TelemetryToken:
    """The token ``microcosm-emitter telemetry-token`` printed."""

    access_token: str = field(repr=False)
    expires_at: str
    collector_origin: str

    @property
    def expiry(self) -> datetime:
        """``expires_at`` as a time with its time zone."""
        return datetime.fromisoformat(self.expires_at)


def emitter_version(
    *,
    command: str | os.PathLike[str] | None = None,
    timeout: float = VERSION_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> EmitterVersion | CommandFailure:
    """Not implemented yet: returns ``could_not_run``."""
    del command, timeout, environment
    return CommandFailure("version", "could_not_run")


def emitter_status(
    *,
    event_queue_file: str | os.PathLike[str] | None = None,
    command: str | os.PathLike[str] | None = None,
    timeout: float = STATUS_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> EmitterStatus | CommandFailure:
    """Not implemented yet: returns ``could_not_run``."""
    del event_queue_file, command, timeout, environment
    return CommandFailure("status", "could_not_run")


def telemetry_token(
    lifetime_seconds: int,
    *,
    development_collector_url: str | None = None,
    command: str | os.PathLike[str] | None = None,
    timeout: float = TELEMETRY_TOKEN_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> TelemetryToken | CommandFailure:
    """Not implemented yet: returns ``could_not_run``."""
    del lifetime_seconds, development_collector_url, command, timeout, environment
    return CommandFailure("telemetry-token", "could_not_run")


def recorded_command_output(command: CommandName) -> str:
    """Return the recorded standard output of ``command``: one JSON line."""
    if command not in COMMAND_NAMES:
        error_message = "Unknown emitter command."
        raise ValueError(error_message)
    recorded = (
        files("microcosm_emitter_interface")
        .joinpath("fixtures", COMMAND_FIXTURE_DIRECTORY, f"{command}.json")
        .read_text(encoding="utf-8")
    )
    return json.dumps(json.loads(recorded)) + "\n"

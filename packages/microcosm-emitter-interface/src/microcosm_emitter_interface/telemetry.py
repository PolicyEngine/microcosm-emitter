"""Typed telemetry events, sent unchanged to the emitter's telemetry module.

Each command below becomes exactly the message the emitter's own in-process
client (``microcosm_emitter.telemetry.client``) sends, so a build's telemetry
does not change when it moves to this interface. The emitter's service, not
this package, timestamps, sanitizes, persists and delivers the events.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from microcosm_emitter_interface.serialization import wire_value

if TYPE_CHECKING:
    from microcosm_emitter_interface.client import EmitterClient

type TelemetryEventType = Literal[
    "run", "stage", "progress", "calibration", "heartbeat"
]
type TelemetryStatus = Literal["started", "progress", "completed", "failed", "blocked"]

DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_FAILURE_CLASS = "build_failure"


@dataclass(frozen=True)
class TelemetryRun:
    """Identity registered with the hosted collector for one build."""

    run_id: str
    country_code: str
    pipeline: str
    candidate_id: str | None = None
    release_id: str | None = None
    run_kind: str = "build"
    producer_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_registration(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "producer_id": self.producer_id,
            "country_code": self.country_code,
            "pipeline": self.pipeline,
            "candidate_id": self.candidate_id,
            "release_id": self.release_id,
            "run_kind": self.run_kind,
        }


@dataclass(frozen=True)
class TelemetrySettings:
    """Configuration for the emitter's telemetry module for one build."""

    run: TelemetryRun
    identity: Mapping[str, Any] = field(default_factory=dict)
    development_collector_url: str | None = None
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS

    def configuration(self, spool_path: Path) -> dict[str, Any]:
        return wire_value(
            {
                "registration": self.run.as_registration(),
                "spool_path": str(spool_path),
                "identity": dict(self.identity),
                "development_collector_url": self.development_collector_url,
                "heartbeat_seconds": self.heartbeat_seconds,
            }
        )


@dataclass(frozen=True)
class EventRecord:
    """Any event, with its type and status chosen by the caller."""

    event_type: TelemetryEventType
    status: TelemetryStatus
    stage_id: str | None = None
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageEvent:
    """One stage event; the service does not finish the previous stage."""

    stage_id: str
    status: TelemetryStatus = "started"
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageTransition:
    """Finish the current stage, if any, and start ``stage_id``."""

    stage_id: str
    status: str = "running"
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageProgress:
    stage_id: str
    done: int
    total: int
    unit: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibrationProgress:
    """One calibration epoch; ``transition`` also moves to the calibrating stage."""

    event: Mapping[str, Any]
    transition: bool = False


@dataclass(frozen=True)
class BuildFailure:
    error_type: str
    message: str | None = None
    failed_during: str | None = None
    failure_class: str = DEFAULT_FAILURE_CLASS
    error_code: str | None = None


@dataclass(frozen=True)
class BuildRefused:
    """The build's validation checks refused its candidate at ``phase``.

    On the wire these are the telemetry protocol's existing fields
    ``blocking_gate_ids``, ``blocking_failure_count`` and ``gate_statuses``:
    the identifiers of the refusing checks, how many failed, and each check's
    status.
    """

    phase: str
    refusing_check_ids: Sequence[str]
    refusing_check_count: int | None = None
    check_statuses: Mapping[str, str] | None = None


@dataclass(frozen=True)
class BuildCompleted:
    pass


type TelemetryCommand = (
    EventRecord
    | StageEvent
    | StageTransition
    | StageProgress
    | CalibrationProgress
    | BuildFailure
    | BuildRefused
    | BuildCompleted
)


def telemetry_message(command: TelemetryCommand) -> dict[str, Any]:
    """The telemetry module's request for one typed command."""
    match command:
        case EventRecord():
            message: dict[str, Any] = {
                "command": "emit",
                "event_type": command.event_type,
                "stage_id": command.stage_id,
                "status": command.status,
                "message": command.message,
                "details": dict(command.details),
            }
        case StageEvent():
            message = {
                "command": "stage",
                "stage_id": command.stage_id,
                "status": command.status,
                "message": command.message,
                "details": dict(command.details),
            }
        case StageTransition():
            message = {
                "command": "transition_stage",
                "stage_id": command.stage_id,
                "status": command.status,
                "message": command.message,
                "details": dict(command.details),
            }
        case StageProgress():
            message = {
                "command": "progress",
                "stage_id": command.stage_id,
                "done": command.done,
                "total": command.total,
                "unit": command.unit,
                "details": dict(command.details),
            }
        case CalibrationProgress():
            message = {
                "command": (
                    "transition_calibration_progress"
                    if command.transition
                    else "calibration_progress"
                ),
                "event": dict(command.event),
            }
        case BuildFailure():
            message = {
                "command": "fail",
                "message": command.message,
                "error_type": command.error_type,
                "failed_during": command.failed_during,
                "failure_class": command.failure_class,
            }
            if command.error_code is not None:
                message["error_code"] = command.error_code
        case BuildRefused():
            message = {
                "command": "block",
                "phase": command.phase,
                "blocking_gate_ids": list(command.refusing_check_ids),
                "blocking_failure_count": command.refusing_check_count,
                "gate_statuses": (
                    None
                    if command.check_statuses is None
                    else dict(command.check_statuses)
                ),
            }
        case BuildCompleted():
            message = {"command": "complete"}
        case _:
            error_message = f"Unsupported telemetry command {type(command).__name__}."
            raise TypeError(error_message)
    return wire_value(message)


class TelemetryRecorder:
    """The telemetry calls a build makes, each one ``emit_telemetry`` request.

    Finishing the run (``complete``, ``fail`` or ``refuse``) does not stop the
    emitter: other hand-overs may follow. The build closes the client.
    """

    def __init__(self, client: EmitterClient) -> None:
        self._client = client

    def emit(
        self,
        *,
        event_type: TelemetryEventType,
        status: TelemetryStatus,
        stage_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._client.emit_telemetry(
            EventRecord(
                event_type,
                status,
                stage_id,
                message,
                {} if details is None else details,
            )
        )

    def stage(
        self,
        stage_id: str,
        *,
        status: TelemetryStatus = "started",
        message: str | None = None,
        **details: Any,
    ) -> bool:
        return self._client.emit_telemetry(
            StageEvent(stage_id, status, message, details)
        )

    def transition_stage(
        self,
        stage_id: str,
        *,
        status: str = "running",
        message: str | None = None,
        **details: Any,
    ) -> bool:
        return self._client.emit_telemetry(
            StageTransition(stage_id, status, message, details)
        )

    def progress(
        self,
        stage_id: str,
        *,
        done: int,
        total: int,
        unit: str | None = None,
        **details: Any,
    ) -> bool:
        return self._client.emit_telemetry(
            StageProgress(stage_id, done, total, unit, details)
        )

    def calibration_progress(self, event: Mapping[str, Any]) -> bool:
        return self._client.emit_telemetry(CalibrationProgress(event))

    def transition_calibration_progress(self, event: Mapping[str, Any]) -> bool:
        return self._client.emit_telemetry(CalibrationProgress(event, transition=True))

    def fail(
        self,
        error: BaseException,
        *,
        failed_during: str | None = None,
        failure_class: str = DEFAULT_FAILURE_CLASS,
        error_code: str | None = None,
    ) -> bool:
        try:
            text = str(error)
        except Exception:
            text = None
        return self._client.emit_telemetry(
            BuildFailure(
                error_type=type(error).__name__,
                message=text,
                failed_during=failed_during,
                failure_class=failure_class,
                error_code=error_code,
            )
        )

    def refuse(
        self,
        *,
        phase: str,
        refusing_check_ids: Sequence[str],
        refusing_check_count: int | None = None,
        check_statuses: Mapping[str, str] | None = None,
    ) -> bool:
        """Record that the build's validation checks refused its candidate."""
        return self._client.emit_telemetry(
            BuildRefused(
                phase, refusing_check_ids, refusing_check_count, check_statuses
            )
        )

    def complete(self) -> bool:
        return self._client.emit_telemetry(BuildCompleted())

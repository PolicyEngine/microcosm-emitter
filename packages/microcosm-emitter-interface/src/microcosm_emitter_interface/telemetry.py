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
        """Return the registration the emitter sends to the collector for this run."""
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
        """Return the module's startup settings for the queue at ``spool_path``.

        The values are converted with ``wire_value``.
        """
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
    """A sequential stage update for ``stage_id``, with its ``status``.

    The emitter maps ``running`` and ``started`` to a start, ``completed`` and
    ``passed`` to completed, ``failed`` to failed, and any other status to
    progress. An update for a stage other than the current one finishes the
    current one first. A start makes ``stage_id`` the current stage, or, when
    it already is, is recorded as progress; completed or failed ends it when
    it is the current stage. No other update makes a stage current.
    """

    stage_id: str
    status: str = "running"
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageProgress:
    """Progress within one stage: ``done`` of ``total``, in the optional ``unit``."""

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
    """The build failed: the error's type name, its message and where it failed.

    Without ``failed_during``, the service uses the current or last stage.
    ``error_code`` is sent only when given.
    """

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
    """The build completed; the service finishes the current stage, if any."""


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
    """Return the telemetry module's request for one typed command.

    The values are converted with ``wire_value``. Any other value raises
    ``TypeError``.
    """
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
        """Hold the client whose ``emit_telemetry`` sends each event."""
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
        """Send an event with the given type and status; True once committed."""
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
        """Send one stage event; True once committed.

        The keyword arguments beyond ``status`` and ``message`` become the
        event's details. The previous stage is not finished.
        """
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
        """Send a sequential stage update for ``stage_id``.

        An update for a stage other than the current one finishes the current
        one first. A ``status`` of ``running`` (the default) or ``started``
        makes ``stage_id`` the current stage, and is recorded as progress when
        it already is; ``completed``, ``passed`` or ``failed`` ends it when it
        is the current stage; any other status is recorded as progress, and
        none of these makes a stage current. Return True once the emitter
        committed the update. The keyword arguments beyond ``status`` and
        ``message`` become its details.
        """
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
        """Send ``done`` of ``total`` progress within a stage; True once committed.

        The keyword arguments beyond ``done``, ``total`` and ``unit`` become
        the event's details.
        """
        return self._client.emit_telemetry(
            StageProgress(stage_id, done, total, unit, details)
        )

    def calibration_progress(self, event: Mapping[str, Any]) -> bool:
        """Send one calibration epoch's event.

        The emitter records ``event`` only when its ``kind`` is
        ``calibration_epoch``, and ignores any other. Return True once the
        emitter accepted the request, also when it ignored the event.
        """
        return self._client.emit_telemetry(CalibrationProgress(event))

    def transition_calibration_progress(self, event: Mapping[str, Any]) -> bool:
        """Send one calibration epoch's event as a stage transition.

        For an event whose ``kind`` is ``calibration_epoch``, the emitter first
        moves the run to the calibrating stage, unless it is already there, then
        records the event; it ignores any other event. Return True once the
        emitter accepted the request, also when it ignored the event.
        """
        return self._client.emit_telemetry(CalibrationProgress(event, transition=True))

    def fail(
        self,
        error: BaseException,
        *,
        failed_during: str | None = None,
        failure_class: str = DEFAULT_FAILURE_CLASS,
        error_code: str | None = None,
    ) -> bool:
        """Record that the build failed with ``error``; True once committed.

        Of the error, only its type name and its ``str`` text are sent; the
        text is None when ``str`` raises.
        """
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
        """Record that the build completed; True once committed."""
        return self._client.emit_telemetry(BuildCompleted())

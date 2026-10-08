"""Best-effort telemetry client; persistence and authentication live in the child process."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from policyengine_local_service.launcher import ServiceHandle, start_service

from policyengine_telemetry.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    QUEUE_WARNING,
    SERVICE_START_WARNING,
    TELEMETRY_MODULE,
)
from policyengine_telemetry.protocol import (
    BUILD_COMPLETED_MESSAGE,
    BUILD_STARTED_MESSAGE,
    CALIBRATION_EVENT_KIND,
    EVENT_TYPE_CALIBRATION,
    EVENT_TYPE_PROGRESS,
    EVENT_TYPE_RUN,
    EVENT_TYPE_STAGE,
    MAX_TELEMETRY_MESSAGE_CHARS,
    SEQUENTIAL_STATUS_MAP,
    STAGE_CALIBRATING,
    STAGE_COMPLETE,
    STAGE_CREATED,
    STAGE_FAILED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROGRESS,
    STATUS_STARTED,
    TelemetryEventType,
    TelemetryStatus,
)
from policyengine_telemetry.sanitization import sanitize_details, sanitize_text


class Transport(Protocol):
    """Small transport interface for instrumentation and deterministic tests."""

    def send(self, message: Mapping[str, Any]) -> None: ...
    def close(self) -> None: ...


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


class LocalTelemetryEmitter:
    """One build's lifecycle, with bounded local sends and no network I/O."""

    def __init__(
        self,
        *,
        run: TelemetryRun,
        transport: Transport | None,
        handle: ServiceHandle | None = None,
    ) -> None:
        self.run = run
        self._transport = transport
        self._handle = handle
        self._closed = False
        self._warned = False
        self._transition_stage: str | None = None

    @classmethod
    def start(
        cls,
        *,
        run: TelemetryRun,
        spool_path: Path | str,
        identity: Mapping[str, Any] | None = None,
        development_collector_url: str | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        startup_timeout_seconds: float = 3.0,
    ) -> LocalTelemetryEmitter:
        """Start the installed service; return a disabled handle if it cannot start."""
        try:
            handle = start_service(
                TELEMETRY_MODULE,
                {
                    "registration": run.as_registration(),
                    "spool_path": str(spool_path),
                    "development_collector_url": development_collector_url,
                    "heartbeat_seconds": heartbeat_seconds,
                },
                startup_timeout=startup_timeout_seconds,
            )
        except Exception as error:
            print(
                SERVICE_START_WARNING.format(error_type=type(error).__name__),
                file=sys.stderr,
            )
            return cls(run=run, transport=None)
        emitter = cls(run=run, transport=handle.client, handle=handle)
        emitter.emit(
            event_type=EVENT_TYPE_RUN,
            stage_id=STAGE_CREATED,
            status=STATUS_STARTED,
            message=BUILD_STARTED_MESSAGE,
            details={"identity": identity or {}},
        )
        return emitter

    @property
    def available(self) -> bool:
        return self._transport is not None and not self._closed

    def emit(
        self,
        *,
        event_type: TelemetryEventType,
        status: TelemetryStatus,
        stage_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Sanitize and enqueue one event without raising into the build."""
        if not self.available:
            return
        try:
            self._transport.send(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event_type": event_type,
                    "stage_id": stage_id,
                    "status": status,
                    "message": sanitize_text(message, limit=MAX_TELEMETRY_MESSAGE_CHARS)
                    if message
                    else None,
                    "details": sanitize_details(details or {}),
                }
            )
        except Exception as error:
            self._warn(error)

    def _warn(self, error: Exception) -> None:
        if not self._warned:
            print(
                QUEUE_WARNING.format(error_type=type(error).__name__), file=sys.stderr
            )
            self._warned = True

    def stage(
        self,
        stage_id: str,
        *,
        status: TelemetryStatus = STATUS_STARTED,
        message: str | None = None,
        **details: Any,
    ) -> None:
        self.emit(
            event_type=EVENT_TYPE_STAGE,
            stage_id=stage_id,
            status=status,
            message=message,
            details=details,
        )

    def transition_stage(
        self,
        stage_id: str,
        *,
        status: str = "running",
        message: str | None = None,
        **details: Any,
    ) -> None:
        """Translate a sequential stage update into explicit lifecycle events."""

        collector_status = SEQUENTIAL_STATUS_MAP.get(status, STATUS_PROGRESS)
        if collector_status == STATUS_STARTED:
            if self._transition_stage == stage_id:
                self.stage(
                    stage_id,
                    status=STATUS_PROGRESS,
                    message=message,
                    **details,
                )
                return
            self._close_transition_stage()
            self._transition_stage = stage_id
        elif self._transition_stage == stage_id:
            self._transition_stage = None
        else:
            self._close_transition_stage()
        self.stage(
            stage_id,
            status=collector_status,
            message=message,
            **details,
        )

    def progress(
        self,
        stage_id: str,
        *,
        done: int,
        total: int,
        unit: str | None = None,
        **details: Any,
    ) -> None:
        self.emit(
            event_type=EVENT_TYPE_PROGRESS,
            stage_id=stage_id,
            status=STATUS_PROGRESS,
            details={"done": done, "total": total, "unit": unit, **details},
        )

    def calibration_progress(self, event: Mapping[str, Any]) -> None:
        if event.get("kind") != CALIBRATION_EVENT_KIND:
            return
        self.emit(
            event_type=EVENT_TYPE_CALIBRATION,
            stage_id=STAGE_CALIBRATING,
            status=STATUS_PROGRESS,
            details=event,
        )

    def transition_calibration_progress(self, event: Mapping[str, Any]) -> None:
        """Enter the sequential calibration stage, then report one epoch."""

        if event.get("kind") != CALIBRATION_EVENT_KIND:
            return
        if self._transition_stage != STAGE_CALIBRATING:
            self.transition_stage(STAGE_CALIBRATING)
        self.calibration_progress(event)

    def fail(
        self,
        error: BaseException,
        *,
        failed_during: str | None = None,
        failure_class: str = "build_failure",
    ) -> None:
        failed_stage = failed_during or self._transition_stage
        message = str(error)[:MAX_TELEMETRY_MESSAGE_CHARS]
        details = {
            "error_type": type(error).__name__,
            "failure_class": failure_class,
            "failed_during": failed_stage,
        }
        self._close_transition_stage(status=STATUS_FAILED, message=message, **details)
        self.emit(
            event_type=EVENT_TYPE_RUN,
            stage_id=STAGE_FAILED,
            status=STATUS_FAILED,
            message=message,
            details=details,
        )
        self.close()

    def complete(self) -> None:
        self._close_transition_stage()
        self.emit(
            event_type=EVENT_TYPE_RUN,
            stage_id=STAGE_COMPLETE,
            status=STATUS_COMPLETED,
            message=BUILD_COMPLETED_MESSAGE,
        )
        self.close()

    def close(self) -> None:
        """Ask the service to flush in the background, then release the handle."""

        if self._closed:
            return
        try:
            if self._handle is not None:
                self._handle.close()
            elif self._transport is not None:
                self._transport.close()
        except Exception as error:
            self._warn(error)
        finally:
            self._closed = True

    def _close_transition_stage(
        self,
        *,
        status: TelemetryStatus = STATUS_COMPLETED,
        message: str | None = None,
        **details: Any,
    ) -> None:
        if self._transition_stage is None:
            return
        stage_id = self._transition_stage
        self._transition_stage = None
        self.stage(stage_id, status=status, message=message, **details)

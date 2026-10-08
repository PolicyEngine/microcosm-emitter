"""Thin build adapter: start the service, send requests, and close the connection."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from microcosm_provider_client.launcher import ServiceHandle, start_service

from microcosm_provider_telemetry.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    QUEUE_WARNING,
    SERVICE_START_WARNING,
    TELEMETRY_MODULE,
)
from microcosm_provider_telemetry.protocol import (
    STATUS_STARTED,
    TelemetryEventType,
    TelemetryStatus,
)
from microcosm_provider_telemetry.requests import (
    COMMAND_CALIBRATION_PROGRESS,
    COMMAND_COMPLETE,
    COMMAND_EMIT,
    COMMAND_FAIL,
    COMMAND_PROGRESS,
    COMMAND_STAGE,
    COMMAND_TRANSITION_CALIBRATION_PROGRESS,
    COMMAND_TRANSITION_STAGE,
    DEFAULT_FAILURE_CLASS,
)
from microcosm_provider_telemetry.serialization import wire_value


class Transport(Protocol):
    """Request transport, replaceable without starting a process in unit tests."""

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
    """Send bounded local requests; telemetry processing runs in the service."""

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
        """Start the installed service; return a disabled adapter on failure."""
        try:
            handle = start_service(
                TELEMETRY_MODULE,
                wire_value(
                    {
                        "registration": run.as_registration(),
                        "spool_path": str(spool_path),
                        "identity": identity or {},
                        "development_collector_url": development_collector_url,
                        "heartbeat_seconds": heartbeat_seconds,
                    }
                ),
                startup_timeout=startup_timeout_seconds,
            )
        except Exception as error:
            print(
                SERVICE_START_WARNING.format(error_type=type(error).__name__),
                file=sys.stderr,
            )
            return cls(run=run, transport=None)
        return cls(run=run, transport=handle.client, handle=handle)

    @property
    def available(self) -> bool:
        return self._transport is not None and not self._closed

    def _request(self, command: str, **arguments: Any) -> None:
        if not self.available:
            return
        try:
            self._transport.send(wire_value({"command": command, **arguments}))
        except Exception as error:
            self._warn(error)

    def _warn(self, error: Exception) -> None:
        if not self._warned:
            print(
                QUEUE_WARNING.format(error_type=type(error).__name__), file=sys.stderr
            )
            self._warned = True

    def emit(
        self,
        *,
        event_type: TelemetryEventType,
        status: TelemetryStatus,
        stage_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._request(
            COMMAND_EMIT,
            event_type=event_type,
            stage_id=stage_id,
            status=status,
            message=message,
            details=details or {},
        )

    def stage(
        self,
        stage_id: str,
        *,
        status: TelemetryStatus = STATUS_STARTED,
        message: str | None = None,
        **details: Any,
    ) -> None:
        self._request(
            COMMAND_STAGE,
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
        self._request(
            COMMAND_TRANSITION_STAGE,
            stage_id=stage_id,
            status=status,
            message=message,
            details=details,
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
        self._request(
            COMMAND_PROGRESS,
            stage_id=stage_id,
            done=done,
            total=total,
            unit=unit,
            details=details,
        )

    def calibration_progress(self, event: Mapping[str, Any]) -> None:
        self._request(COMMAND_CALIBRATION_PROGRESS, event=event)

    def transition_calibration_progress(self, event: Mapping[str, Any]) -> None:
        self._request(COMMAND_TRANSITION_CALIBRATION_PROGRESS, event=event)

    def fail(
        self,
        error: BaseException,
        *,
        failed_during: str | None = None,
        failure_class: str = DEFAULT_FAILURE_CLASS,
    ) -> None:
        try:
            try:
                message = str(error)
            except Exception as formatting_error:
                self._warn(formatting_error)
                message = None
            self._request(
                COMMAND_FAIL,
                message=message,
                error_type=type(error).__name__,
                failed_during=failed_during,
                failure_class=failure_class,
            )
        finally:
            self.close()

    def complete(self) -> None:
        try:
            self._request(COMMAND_COMPLETE)
        finally:
            self.close()

    def close(self) -> None:
        """Request a bounded background drain without inventing a build outcome."""
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

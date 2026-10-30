"""Thin build adapter: start the service, send requests, and close the connection.

The run identity, the configuration and every message come from
``microcosm_emitter_interface.telemetry``, so this in-process client and the
interface package send identical messages. Each message is built inside the
error handling: an argument that cannot be converted to JSON prints one warning
and the build continues.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from microcosm_emitter.host.constants import STARTUP_TIMEOUT
from microcosm_emitter.host.errors import ServiceStartupError, safe_startup_error
from microcosm_emitter.host.launcher import ServiceHandle, start_service
from microcosm_emitter.telemetry.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    QUEUE_WARNING,
    SERVICE_START_WARNING,
    TELEMETRY_MODULE,
)
from microcosm_emitter.telemetry.protocol import (
    STATUS_STARTED,
    TelemetryEventType,
    TelemetryStatus,
)
from microcosm_emitter.telemetry.requests import DEFAULT_FAILURE_CLASS
from microcosm_emitter_interface.telemetry import (
    BuildCompleted,
    BuildFailure,
    BuildRefused,
    CalibrationProgress,
    EventRecord,
    StageEvent,
    StageProgress,
    StageTransition,
    TelemetryCommand,
    TelemetrySettings,
    telemetry_message,
)
from microcosm_emitter_interface.telemetry import TelemetryRun as TelemetryRun


class Transport(Protocol):
    """Request transport, replaceable without starting a process in unit tests."""

    def send(self, message: Mapping[str, Any]) -> None:
        """Send one message to the telemetry module, raising if it is not accepted."""
        ...

    def close(self) -> None:
        """Close the transport; the emitter sends nothing through it afterwards."""
        ...


class LocalTelemetryEmitter:
    """Send bounded local requests; telemetry processing runs in the service."""

    def __init__(
        self,
        *,
        run: TelemetryRun,
        transport: Transport | None,
        handle: ServiceHandle | None = None,
        close_service_on_finish: bool = True,
    ) -> None:
        """Hold the run, its transport and the service handle, if any.

        Without a transport the emitter is not ``available`` and sends nothing.
        With ``close_service_on_finish``, ``fail``, ``block`` and ``complete`` close
        the service after their request.
        """
        self.run = run
        self._transport = transport
        self._handle = handle
        self._close_service_on_finish = close_service_on_finish
        self._closed = False
        self._warned = False
        self.startup_error: ServiceStartupError | None = None

    @classmethod
    def start(
        cls,
        *,
        run: TelemetryRun,
        spool_path: Path | str,
        identity: Mapping[str, Any] | None = None,
        development_collector_url: str | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        startup_timeout_seconds: float = STARTUP_TIMEOUT,
        close_service_on_finish: bool = True,
    ) -> LocalTelemetryEmitter:
        """Start the installed service; return a disabled adapter on failure."""
        try:
            settings = TelemetrySettings(
                run=run,
                identity={} if identity is None else identity,
                development_collector_url=development_collector_url,
                heartbeat_seconds=heartbeat_seconds,
            )
            handle = start_service(
                TELEMETRY_MODULE,
                settings.configuration(Path(spool_path)),
                startup_timeout=startup_timeout_seconds,
            )
        except Exception as error:
            failure = safe_startup_error(error)
            print(
                SERVICE_START_WARNING.format(error_type=str(failure)),
                file=sys.stderr,
            )
            emitter = cls(
                run=run, transport=None, close_service_on_finish=close_service_on_finish
            )
            emitter.startup_error = failure
            return emitter
        return cls(
            run=run,
            transport=handle.client,
            handle=handle,
            close_service_on_finish=close_service_on_finish,
        )

    @property
    def available(self) -> bool:
        """Whether the emitter has a transport and is not closed."""
        return self._transport is not None and not self._closed

    def _request(self, command: TelemetryCommand) -> None:
        if not self.available:
            return
        try:
            self._transport.send(telemetry_message(command))
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
        """Send one event whose type and status the caller chooses."""
        self._request(
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
        status: TelemetryStatus = STATUS_STARTED,
        message: str | None = None,
        **details: Any,
    ) -> None:
        """Send one stage event; the service does not finish the previous stage."""
        self._request(StageEvent(stage_id, status, message, details))

    def transition_stage(
        self,
        stage_id: str,
        *,
        status: str = "running",
        message: str | None = None,
        **details: Any,
    ) -> None:
        """Send a sequential stage update for ``stage_id``.

        An update for a stage other than the current one finishes the current one
        first. The service maps ``status`` to a telemetry status.
        """
        self._request(StageTransition(stage_id, status, message, details))

    def progress(
        self,
        stage_id: str,
        *,
        done: int,
        total: int,
        unit: str | None = None,
        **details: Any,
    ) -> None:
        """Send progress for ``stage_id``: ``done`` of ``total``, in ``unit``."""
        self._request(StageProgress(stage_id, done, total, unit, details))

    def calibration_progress(self, event: Mapping[str, Any]) -> None:
        """Send one calibration progress ``event``."""
        self._request(CalibrationProgress(event))

    def transition_calibration_progress(self, event: Mapping[str, Any]) -> None:
        """Send one calibration progress ``event`` as a stage transition.

        The service first moves the run to the calibrating stage, unless it is
        already there.
        """
        self._request(CalibrationProgress(event, transition=True))

    def fail(
        self,
        error: BaseException,
        *,
        failed_during: str | None = None,
        failure_class: str = DEFAULT_FAILURE_CLASS,
        error_code: str | None = None,
    ) -> None:
        """Finish the run as failed, with the type and text of ``error``.

        When ``str(error)`` raises, the failure is sent without text. With
        ``close_service_on_finish``, the service is closed afterwards.
        """
        try:
            try:
                message = str(error)
            except Exception as formatting_error:
                self._warn(formatting_error)
                message = None
            self._request(
                BuildFailure(
                    error_type=type(error).__name__,
                    message=message,
                    failed_during=failed_during,
                    failure_class=failure_class,
                    error_code=error_code,
                )
            )
        finally:
            if self._close_service_on_finish:
                self.close()

    def block(
        self,
        *,
        phase: str,
        blocking_gate_ids: list[str] | tuple[str, ...],
        blocking_failure_count: int | None = None,
        gate_statuses: Mapping[str, str] | None = None,
    ) -> None:
        """Ask the service to finish a run refused by its validation checks."""
        try:
            # The interface names these fields for what they hold: the
            # refusing validation checks, how many failed, and each status.
            self._request(
                BuildRefused(
                    phase=phase,
                    refusing_check_ids=blocking_gate_ids,
                    refusing_check_count=blocking_failure_count,
                    check_statuses=gate_statuses,
                )
            )
        finally:
            if self._close_service_on_finish:
                self.close()

    def complete(self) -> None:
        """Finish the run as completed.

        With ``close_service_on_finish``, the service is closed afterwards.
        """
        try:
            self._request(BuildCompleted())
        finally:
            if self._close_service_on_finish:
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

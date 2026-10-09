"""Interpret build requests without mutating state until their events commit."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from microcosm_emitter.telemetry.protocol import (
    BUILD_COMPLETED_MESSAGE,
    BUILD_STARTED_MESSAGE,
    CALIBRATION_EVENT_KIND,
    EVENT_TYPE_CALIBRATION,
    EVENT_TYPE_HEARTBEAT,
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
    TELEMETRY_IDENTIFIER_PATTERN,
)
from microcosm_emitter.telemetry.requests import (
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
from microcosm_emitter.telemetry.service.sanitization import (
    sanitize_details,
    sanitize_text,
)
from microcosm_emitter.telemetry.service.timestamps import utc_now

_EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_RUN,
        EVENT_TYPE_STAGE,
        EVENT_TYPE_PROGRESS,
        EVENT_TYPE_CALIBRATION,
        EVENT_TYPE_HEARTBEAT,
    }
)
_STATUSES = frozenset(
    {STATUS_STARTED, STATUS_PROGRESS, STATUS_COMPLETED, STATUS_FAILED}
)


@dataclass(frozen=True)
class LifecycleState:
    """State corresponding exclusively to events already committed to the queue."""

    transition_stage: str | None = None
    last_stage: str = STAGE_CREATED
    finished: bool = False


def started_event(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Create the initial event in the service, before socket readiness."""
    return _event(
        EVENT_TYPE_RUN,
        STATUS_STARTED,
        STAGE_CREATED,
        BUILD_STARTED_MESSAGE,
        {"identity": identity},
    )


def heartbeat_event(state: LifecycleState) -> dict[str, Any]:
    """Validate the stage identity without rewriting periodic events."""
    return _event(EVENT_TYPE_HEARTBEAT, STATUS_PROGRESS, state.last_stage)


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("telemetry details must be an object")
    return value


def _optional_text(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError("telemetry text must be a string or null")
    return value


def _stage_id(request: Mapping[str, Any]) -> str:
    value = _optional_stage_id(request.get("stage_id"))
    if value is None:
        raise ValueError("stage_id must be a nonempty string")
    return value


def _optional_stage_id(value: Any) -> str | None:
    """Preserve stage identities and reject values the collector cannot accept."""
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(TELEMETRY_IDENTIFIER_PATTERN, value) is None
    ):
        raise ValueError("stage_id must match the collector identifier contract")
    return value


def _event(
    event_type: str,
    status: str,
    stage_id: str | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
        raise ValueError("unsupported telemetry event type")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("unsupported telemetry status")
    stage_id, message = _optional_stage_id(stage_id), _optional_text(message)
    return {
        "timestamp": utc_now(),
        "event_type": event_type,
        "stage_id": stage_id,
        "status": status,
        "message": sanitize_text(message, limit=MAX_TELEMETRY_MESSAGE_CHARS)
        if message
        else None,
        "details": sanitize_details(_mapping(details if details is not None else {})),
    }


def process_request(
    state: LifecycleState, request: Mapping[str, Any]
) -> tuple[LifecycleState, list[dict[str, Any]]]:
    """Return proposed state and events; the caller commits before adopting state."""
    if state.finished:
        return state, []
    processor = _RequestProcessor(state)
    processor.process(request)
    return processor.state, processor.events


class _RequestProcessor:
    def __init__(self, state: LifecycleState) -> None:
        self.state = state
        self.events: list[dict[str, Any]] = []

    def process(self, request: Mapping[str, Any]) -> None:
        command = request.get("command")
        if command == COMMAND_EMIT:
            self.add(
                request.get("event_type"),
                request.get("status"),
                request.get("stage_id"),
                request.get("message"),
                request.get("details", {}),
            )
        elif command == COMMAND_STAGE:
            self.add(
                EVENT_TYPE_STAGE,
                request.get("status", STATUS_STARTED),
                _stage_id(request),
                request.get("message"),
                request.get("details", {}),
            )
        elif command == COMMAND_TRANSITION_STAGE:
            self.transition(request)
        elif command == COMMAND_PROGRESS:
            self.add(
                EVENT_TYPE_PROGRESS,
                STATUS_PROGRESS,
                _stage_id(request),
                details={
                    "done": request["done"],
                    "total": request["total"],
                    "unit": request.get("unit"),
                    **_mapping(request.get("details", {})),
                },
            )
        elif command in {
            COMMAND_CALIBRATION_PROGRESS,
            COMMAND_TRANSITION_CALIBRATION_PROGRESS,
        }:
            event = _mapping(request.get("event"))
            if event.get("kind") == CALIBRATION_EVENT_KIND:
                if command == COMMAND_TRANSITION_CALIBRATION_PROGRESS and (
                    self.state.transition_stage != STAGE_CALIBRATING
                ):
                    self.transition({"stage_id": STAGE_CALIBRATING})
                self.add(
                    EVENT_TYPE_CALIBRATION,
                    STATUS_PROGRESS,
                    STAGE_CALIBRATING,
                    details=event,
                )
        elif command == COMMAND_FAIL:
            self.fail(request)
        elif command == COMMAND_COMPLETE:
            self.close_stage()
            self.add(
                EVENT_TYPE_RUN,
                STATUS_COMPLETED,
                STAGE_COMPLETE,
                BUILD_COMPLETED_MESSAGE,
            )
        else:
            raise ValueError("unsupported telemetry command")

    def add(
        self,
        event_type: str,
        status: str,
        stage_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append(_event(event_type, status, stage_id, message, details))
        if event_type == EVENT_TYPE_RUN and status in {STATUS_COMPLETED, STATUS_FAILED}:
            self.state = replace(self.state, finished=True, transition_stage=None)
        elif stage_id is not None:
            self.state = replace(self.state, last_stage=stage_id)

    def transition(self, request: Mapping[str, Any]) -> None:
        stage = _stage_id(request)
        source_status = request.get("status", "running")
        if not isinstance(source_status, str):
            raise TypeError("stage status must be a string")
        status = SEQUENTIAL_STATUS_MAP.get(source_status, STATUS_PROGRESS)
        if status == STATUS_STARTED:
            if self.state.transition_stage == stage:
                status = STATUS_PROGRESS
            else:
                self.close_stage()
                self.state = replace(self.state, transition_stage=stage)
        elif self.state.transition_stage == stage:
            if status in {STATUS_COMPLETED, STATUS_FAILED}:
                self.state = replace(self.state, transition_stage=None)
        else:
            self.close_stage()
        self.add(
            EVENT_TYPE_STAGE,
            status,
            stage,
            request.get("message"),
            request.get("details", {}),
        )

    def close_stage(
        self,
        status: str = STATUS_COMPLETED,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        stage = self.state.transition_stage
        if stage is not None:
            self.add(EVENT_TYPE_STAGE, status, stage, message, details)
            self.state = replace(self.state, transition_stage=None)

    def fail(self, request: Mapping[str, Any]) -> None:
        failed_stage = (
            _optional_text(request.get("failed_during"))
            or self.state.transition_stage
            or self.state.last_stage
        )
        details = {
            "error_type": _optional_text(request.get("error_type")),
            "failure_class": _optional_text(
                request.get("failure_class", DEFAULT_FAILURE_CLASS)
            ),
            "failed_during": failed_stage,
        }
        message = _optional_text(request.get("message"))
        self.close_stage(STATUS_FAILED, message, details)
        self.add(EVENT_TYPE_RUN, STATUS_FAILED, STAGE_FAILED, message, details)

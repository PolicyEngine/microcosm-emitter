"""Interpret build requests without mutating state until their events commit.

Stage spans: the emitter keeps a stack of the stages that started and have not
ended, from every stage event however it is sent. A stage start gets
``details.stage_span`` with the enclosing open stage (``parent_stage_id``) and
how many times a stage of that ID has started (``occurrence``). A stage end
closes the innermost open stage of its ID, after closing the stages opened
inside it, and its ``stage_span`` adds the stage's awake and asleep seconds and
why the emitter closed it (``closed_by``, null when the build ended it). When
the run ends, every open stage is closed, innermost first. ``stage_span``,
``outer`` and the heartbeat's ``reading`` and ``sleep`` are keys the emitter
adds after sanitizing the build's details, which may not use them.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from microcosm_emitter.telemetry.protocol import (
    BUILD_BLOCKED_MESSAGE,
    BUILD_COMPLETED_MESSAGE,
    BUILD_STARTED_MESSAGE,
    CALIBRATION_EVENT_KIND,
    EVENT_TYPE_CALIBRATION,
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_PROGRESS,
    EVENT_TYPE_RUN,
    EVENT_TYPE_STAGE,
    MAX_TELEMETRY_CHECK_ID_CHARS,
    MAX_TELEMETRY_DETAILS_BYTES,
    MAX_TELEMETRY_MESSAGE_CHARS,
    SEQUENTIAL_STATUS_MAP,
    STAGE_BLOCKED,
    STAGE_CALIBRATING,
    STAGE_COMPLETE,
    STAGE_CREATED,
    STAGE_FAILED,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROGRESS,
    STATUS_STARTED,
    TELEMETRY_IDENTIFIER_PATTERN,
)
from microcosm_emitter.telemetry.requests import (
    COMMAND_BLOCK,
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
from microcosm_emitter.telemetry.service.clocks import ClockReading
from microcosm_emitter.telemetry.service.sanitization import (
    details_size,
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
    {STATUS_STARTED, STATUS_PROGRESS, STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED}
)
_STAGE_ENDS = frozenset({STATUS_COMPLETED, STATUS_FAILED})

#: The ``details`` keys only the emitter writes; a build's details may not use
#: them.
EMITTER_DETAIL_KEYS: Final = frozenset(
    {"reading", "sleep", "environment", "stage_span", "outer"}
)
#: The most open stages the emitter tracks; a start beyond them gets no span.
MAX_OPEN_STAGES: Final = 32
#: Why the emitter closed a stage, in its end's ``stage_span.closed_by``.
CLOSED_BY_ENCLOSING_STAGE_ENDED: Final = "enclosing_stage_ended"
CLOSED_BY_NEXT_STAGE_STARTED: Final = "next_stage_started"
CLOSED_BY_OTHER_STAGE_UPDATED: Final = "other_stage_updated"
CLOSED_BY_RUN_COMPLETED: Final = "run_completed"
CLOSED_BY_RUN_BLOCKED: Final = "run_blocked"
CLOSED_BY_RUN_FAILED: Final = "run_failed"
_SECONDS_DIGITS: Final = 3
_MAX_UNIT_CHARS: Final = 200


@dataclass(frozen=True)
class OpenStage:
    """A stage that started and has not ended, and what its end will report."""

    stage_id: str
    occurrence: int
    parent_stage_id: str | None
    started: ClockReading | None


@dataclass(frozen=True)
class ProgressRecord:
    """The latest progress the build reported, as a heartbeat's reading repeats it."""

    stage_id: str
    done: Any
    total: Any
    unit: Any
    outer: Mapping[str, Any] | None
    reported_at: str

    def as_reading(self) -> dict[str, Any]:
        """Return the progress as a reading's ``progress``."""
        return {
            "stage_id": self.stage_id,
            "done": self.done,
            "total": self.total,
            "unit": self.unit,
            "outer": None if self.outer is None else dict(self.outer),
            "reported_at": self.reported_at,
        }


@dataclass(frozen=True)
class LifecycleState:
    """State corresponding exclusively to events already committed to the queue.

    ``open_stages`` lists the open stages, outermost first; ``stage_starts``
    counts the starts of each stage ID.
    """

    transition_stage: str | None = None
    last_stage: str = STAGE_CREATED
    finished: bool = False
    open_stages: tuple[OpenStage, ...] = ()
    stage_starts: Mapping[str, int] = field(default_factory=dict)
    latest_progress: ProgressRecord | None = None


def _with_emitter_keys(
    details: Mapping[str, Any], emitter_details: Mapping[str, Any]
) -> dict[str, Any]:
    """Return sanitized ``details`` with the emitter's own keys added.

    When both together exceed the details limit, the build's part is compacted
    further, so the emitter's keys are kept and the whole stays within it.
    """
    combined = {**details, **emitter_details}
    if details_size(combined) <= MAX_TELEMETRY_DETAILS_BYTES:
        return combined
    budget = MAX_TELEMETRY_DETAILS_BYTES - details_size(emitter_details)
    return {**sanitize_details(details, limit=budget), **emitter_details}


def current_stage(state: LifecycleState) -> str:
    """Return the innermost open stage, or the last stage when none is open."""
    return state.open_stages[-1].stage_id if state.open_stages else state.last_stage


def started_event(
    identity: Mapping[str, Any], environment: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Create the initial event in the service, before socket readiness.

    ``environment`` is the emitter's description of the machine, added beside
    the build's sanitized ``identity``.
    """
    event = _event(
        EVENT_TYPE_RUN,
        STATUS_STARTED,
        STAGE_CREATED,
        BUILD_STARTED_MESSAGE,
        {"identity": identity},
    )
    if environment is not None:
        event["details"] = _with_emitter_keys(
            event["details"], {"environment": dict(environment)}
        )
    return event


def heartbeat_event(
    state: LifecycleState,
    reading: Mapping[str, Any] | None = None,
    sleep: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a heartbeat at the current stage, with its reading and any sleep."""
    event = _event(EVENT_TYPE_HEARTBEAT, STATUS_PROGRESS, current_stage(state))
    emitter_details: dict[str, Any] = {}
    if reading is not None:
        emitter_details["reading"] = dict(reading)
    if sleep is not None:
        emitter_details["sleep"] = dict(sleep)
    event["details"] = _with_emitter_keys(event["details"], emitter_details)
    return event


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        error_message = "telemetry details must be an object"
        raise TypeError(error_message)
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        error_message = "telemetry text must be a string or null"
        raise TypeError(error_message)
    return value


def _stage_id(request: Mapping[str, Any]) -> str:
    value = _optional_stage_id(request.get("stage_id"))
    if value is None:
        error_message = "stage_id must be a nonempty string"
        raise ValueError(error_message)
    return value


def _optional_stage_id(value: object) -> str | None:
    """Preserve stage identities and reject values the collector cannot accept."""
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(TELEMETRY_IDENTIFIER_PATTERN, value) is None
    ):
        error_message = "stage_id must match the collector identifier contract"
        raise ValueError(error_message)
    return value


def _outer(value: object) -> dict[str, Any] | None:
    """Return a progress request's outer counter, or None when it has none."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        error_message = "outer progress must be an object"
        raise TypeError(error_message)
    done, total, unit = value.get("done"), value.get("total"), value.get("unit")
    if any(
        isinstance(count, bool) or not isinstance(count, int) for count in (done, total)
    ):
        error_message = "outer progress needs integer done and total counts"
        raise ValueError(error_message)
    if unit is not None and not isinstance(unit, str):
        error_message = "outer progress unit must be a string or null"
        raise ValueError(error_message)
    return {
        "done": done,
        "total": total,
        "unit": None if unit is None else sanitize_text(unit, limit=_MAX_UNIT_CHARS),
    }


def _validate_block_details(details: Mapping[str, Any]) -> None:
    """Match the collector's required refusal metadata before persistence."""
    phase = details.get("phase")
    count = details.get("blocking_failure_count")
    check_ids = details.get("blocking_gate_ids")
    if (
        not isinstance(phase, str)
        or re.fullmatch(TELEMETRY_IDENTIFIER_PATTERN, phase) is None
    ):
        error_message = "blocked phase must be a safe identifier"
        raise ValueError(error_message)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        error_message = "blocking_failure_count must be an integer of at least 1"
        raise ValueError(error_message)
    if not isinstance(check_ids, list) or any(
        not isinstance(check_id, str)
        or not 1 <= len(check_id) <= MAX_TELEMETRY_CHECK_ID_CHARS
        for check_id in check_ids
    ):
        error_message = (
            "blocking_gate_ids must contain check identifiers of 1 to 200 characters"
        )
        raise ValueError(error_message)


def _refuse_emitter_keys(details: Mapping[str, Any]) -> None:
    used = sorted(EMITTER_DETAIL_KEYS.intersection(str(key) for key in details))
    if used:
        error_message = (
            "telemetry details may not use the emitter's own keys: " + ", ".join(used)
        )
        raise ValueError(error_message)


def _event(
    event_type: str,
    status: str,
    stage_id: str | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
        error_message = "unsupported telemetry event type"
        raise ValueError(error_message)
    if not isinstance(status, str) or status not in _STATUSES:
        error_message = "unsupported telemetry status"
        raise ValueError(error_message)
    stage_id, message = _optional_stage_id(stage_id), _optional_text(message)
    original_details = _mapping(details if details is not None else {})
    _refuse_emitter_keys(original_details)
    if status == STATUS_BLOCKED:
        _validate_block_details(original_details)
    safe_details = sanitize_details(original_details)
    if status == STATUS_BLOCKED:
        # Redaction/size handling must not make a valid block undeliverable.
        _validate_block_details(safe_details)
    return {
        "timestamp": utc_now(),
        "event_type": event_type,
        "stage_id": stage_id,
        "status": status,
        "message": sanitize_text(message, limit=MAX_TELEMETRY_MESSAGE_CHARS)
        if message
        else None,
        "details": safe_details,
    }


def process_request(
    state: LifecycleState,
    request: Mapping[str, Any],
    clock: ClockReading | None = None,
) -> tuple[LifecycleState, list[dict[str, Any]]]:
    """Return proposed state and events; the caller commits before adopting state.

    ``clock`` is the time of the request, which a stage end's span measures
    against its start; without it, the span has no durations.
    """
    if state.finished:
        return state, []
    processor = _RequestProcessor(state, clock)
    processor.process(request)
    return processor.state, processor.events


class _RequestProcessor:
    def __init__(self, state: LifecycleState, clock: ClockReading | None) -> None:
        self.state = state
        self.clock = clock
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
            self.progress(request)
        elif command in {
            COMMAND_CALIBRATION_PROGRESS,
            COMMAND_TRANSITION_CALIBRATION_PROGRESS,
        }:
            self.calibration_progress(command, request)
        elif command == COMMAND_FAIL:
            self.fail(request)
        elif command == COMMAND_BLOCK:
            self.block(request)
        elif command == COMMAND_COMPLETE:
            self.close_all(STATUS_COMPLETED, CLOSED_BY_RUN_COMPLETED)
            self.add(
                EVENT_TYPE_RUN,
                STATUS_COMPLETED,
                STAGE_COMPLETE,
                BUILD_COMPLETED_MESSAGE,
            )
        else:
            error_message = "unsupported telemetry command"
            raise ValueError(error_message)

    def progress(self, request: Mapping[str, Any]) -> None:
        """Record a progress event, with its outer counter, as the latest progress."""
        outer = _outer(request.get("outer"))
        stage_id = _stage_id(request)
        self.add(
            EVENT_TYPE_PROGRESS,
            STATUS_PROGRESS,
            stage_id,
            details={
                "done": request["done"],
                "total": request["total"],
                "unit": request.get("unit"),
                **_mapping(request.get("details", {})),
            },
        )
        event = self.events[-1]
        if outer is not None:
            event["details"] = _with_emitter_keys(event["details"], {"outer": outer})
        details = event["details"]
        self.state = replace(
            self.state,
            latest_progress=ProgressRecord(
                stage_id,
                details.get("done"),
                details.get("total"),
                details.get("unit"),
                outer,
                event["timestamp"],
            ),
        )

    def calibration_progress(self, command: str, request: Mapping[str, Any]) -> None:
        """Record a calibration progress event at ``STAGE_CALIBRATING``.

        The transition command first makes ``STAGE_CALIBRATING`` the
        transition stage, unless it already is. A request whose event is not
        of ``CALIBRATION_EVENT_KIND`` records nothing.
        """
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

    def add(
        self,
        event_type: str,
        status: str,
        stage_id: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one event; a stage start or end also opens or closes its span."""
        span = None
        if event_type == EVENT_TYPE_STAGE and isinstance(stage_id, str):
            if status == STATUS_STARTED:
                span = self._open(stage_id)
            elif status in _STAGE_ENDS:
                span = self._close(stage_id, status, None)
        self._append(_event(event_type, status, stage_id, message, details), span)

    def end_stage(
        self,
        stage_id: str,
        status: str,
        closed_by: str,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the end of a stage the emitter closes, saying why it closed it."""
        span = self._close(stage_id, status, closed_by)
        self._append(_event(EVENT_TYPE_STAGE, status, stage_id, message, details), span)

    def _append(self, event: dict[str, Any], span: Mapping[str, Any] | None) -> None:
        if span is not None:
            event["details"] = _with_emitter_keys(
                event["details"], {"stage_span": dict(span)}
            )
        self.events.append(event)
        event_type, status = event["event_type"], event["status"]
        if event_type == EVENT_TYPE_RUN and status in {
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_BLOCKED,
        }:
            self.state = replace(self.state, finished=True, transition_stage=None)
        elif event["stage_id"] is not None:
            self.state = replace(self.state, last_stage=event["stage_id"])

    def _open(self, stage_id: str) -> dict[str, Any] | None:
        """Open a stage and return its start's span, or None beyond the limit."""
        occurrence = self.state.stage_starts.get(stage_id, 0) + 1
        open_stages = self.state.open_stages
        parent = open_stages[-1].stage_id if open_stages else None
        self.state = replace(
            self.state, stage_starts={**self.state.stage_starts, stage_id: occurrence}
        )
        if len(open_stages) >= MAX_OPEN_STAGES:
            return None
        self.state = replace(
            self.state,
            open_stages=(
                *open_stages,
                OpenStage(stage_id, occurrence, parent, self.clock),
            ),
        )
        return {"parent_stage_id": parent, "occurrence": occurrence}

    def _close(
        self, stage_id: str, status: str, closed_by: str | None
    ) -> dict[str, Any] | None:
        """Close the innermost open stage ``stage_id``; return its end's span.

        The stages opened inside it are ended first, innermost first, with the
        same status. A stage that isn't open has no span.
        """
        open_stages = self.state.open_stages
        position = next(
            (
                index
                for index in range(len(open_stages) - 1, -1, -1)
                if open_stages[index].stage_id == stage_id
            ),
            None,
        )
        if position is None:
            return None
        for nested in reversed(open_stages[position + 1 :]):
            self.end_stage(nested.stage_id, status, CLOSED_BY_ENCLOSING_STAGE_ENDED)
        stage = self.state.open_stages[-1]
        self.state = replace(
            self.state,
            open_stages=self.state.open_stages[:-1],
            transition_stage=(
                None
                if self.state.transition_stage == stage.stage_id
                else self.state.transition_stage
            ),
        )
        return {
            "parent_stage_id": stage.parent_stage_id,
            "occurrence": stage.occurrence,
            **self._durations(stage),
            "closed_by": closed_by,
        }

    def _durations(self, stage: OpenStage) -> dict[str, float | None]:
        """Return the awake and asleep seconds since ``stage`` started."""
        started, ended = stage.started, self.clock
        if started is None or ended is None:
            return {"awake_seconds": None, "asleep_seconds": None}
        awake = ended.awake - started.awake
        asleep = (ended.total - started.total) - awake
        return {
            "awake_seconds": round(awake, _SECONDS_DIGITS),
            "asleep_seconds": round(max(0.0, asleep), _SECONDS_DIGITS),
        }

    def transition(self, request: Mapping[str, Any]) -> None:
        stage = _stage_id(request)
        source_status = request.get("status", "running")
        if not isinstance(source_status, str):
            error_message = "stage status must be a string"
            raise TypeError(error_message)
        status = SEQUENTIAL_STATUS_MAP.get(source_status, STATUS_PROGRESS)
        if status == STATUS_STARTED:
            if self.state.transition_stage == stage:
                status = STATUS_PROGRESS
            else:
                self.close_stage()
                self.state = replace(self.state, transition_stage=stage)
        elif self.state.transition_stage == stage:
            if status in _STAGE_ENDS:
                self.state = replace(self.state, transition_stage=None)
        else:
            self.close_stage(closed_by=CLOSED_BY_OTHER_STAGE_UPDATED)
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
        closed_by: str = CLOSED_BY_NEXT_STAGE_STARTED,
    ) -> None:
        """End the transition stage, if there is one."""
        stage = self.state.transition_stage
        if stage is not None:
            self.end_stage(stage, status, closed_by, message, details)
            self.state = replace(self.state, transition_stage=None)

    def close_all(
        self,
        status: str,
        closed_by: str,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """End every open stage, innermost first, as the run ends.

        The innermost stage carries ``message`` and ``details``, as the run's
        failure does.
        """
        carried = (message, details)
        for stage in reversed(self.state.open_stages):
            self.end_stage(stage.stage_id, status, closed_by, *carried)
            carried = (None, None)
        if self.state.transition_stage is not None:
            # A transition stage started beyond MAX_OPEN_STAGES is not tracked.
            self.close_stage(status, *carried, closed_by=closed_by)
        self.state = replace(self.state, open_stages=(), transition_stage=None)

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
        error_code = _optional_text(request.get("error_code"))
        if error_code is not None:
            details["error_code"] = error_code
        message = _optional_text(request.get("message"))
        self.close_all(STATUS_FAILED, CLOSED_BY_RUN_FAILED, message, details)
        self.add(EVENT_TYPE_RUN, STATUS_FAILED, STAGE_FAILED, message, details)

    def block(self, request: Mapping[str, Any]) -> None:
        check_ids = request.get("blocking_gate_ids")
        if not isinstance(check_ids, list):
            error_message = "blocking_gate_ids must be a list"
            raise TypeError(error_message)
        count = request.get("blocking_failure_count")
        details = {
            "phase": request.get("phase"),
            "blocking_gate_ids": check_ids,
            "blocking_failure_count": len(check_ids) if count is None else count,
        }
        statuses = request.get("gate_statuses")
        if statuses is not None:
            details["gate_statuses"] = _mapping(statuses)
        _validate_block_details(details)
        self.close_all(STATUS_COMPLETED, CLOSED_BY_RUN_BLOCKED)
        self.add(
            EVENT_TYPE_RUN,
            STATUS_BLOCKED,
            STAGE_BLOCKED,
            BUILD_BLOCKED_MESSAGE.format(phase=details["phase"]),
            details,
        )

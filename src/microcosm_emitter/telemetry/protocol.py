"""Shared constants for local and hosted Microcosm telemetry messages."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

# The interface package defines these for both kinds of client.
from microcosm_emitter_interface.telemetry import (
    TelemetryEventType,
    TelemetryStatus,
)

# Every public name, including those re-exported from the interface package,
# so type checkers of this py.typed package treat them as exported.
__all__ = [
    "BUILD_BLOCKED_MESSAGE",
    "BUILD_COMPLETED_MESSAGE",
    "BUILD_STARTED_MESSAGE",
    "CALIBRATION_EVENT_KIND",
    "EVENT_TYPE_CALIBRATION",
    "EVENT_TYPE_HEARTBEAT",
    "EVENT_TYPE_PROGRESS",
    "EVENT_TYPE_RUN",
    "EVENT_TYPE_STAGE",
    "MAX_TELEMETRY_CHECK_ID_CHARS",
    "MAX_TELEMETRY_COLLECTION_ITEMS",
    "MAX_TELEMETRY_DETAILS_BYTES",
    "MAX_TELEMETRY_DETAILS_DEPTH",
    "MAX_TELEMETRY_MESSAGE_CHARS",
    "MAX_TELEMETRY_TEXT_CHARS",
    "SEQUENTIAL_STATUS_MAP",
    "STAGE_BLOCKED",
    "STAGE_CALIBRATING",
    "STAGE_COMPLETE",
    "STAGE_CREATED",
    "STAGE_FAILED",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PROGRESS",
    "STATUS_STARTED",
    "TELEMETRY_IDENTIFIER_PATTERN",
    "TELEMETRY_SCHEMA_VERSION",
    "UNEXPECTED_PROCESS_EXIT_MESSAGE",
    "TelemetryEventType",
    "TelemetryStatus",
]

TELEMETRY_SCHEMA_VERSION: Final = 1
TELEMETRY_IDENTIFIER_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$"

EVENT_TYPE_RUN: Final = "run"
EVENT_TYPE_STAGE: Final = "stage"
EVENT_TYPE_PROGRESS: Final = "progress"
EVENT_TYPE_CALIBRATION: Final = "calibration"
EVENT_TYPE_HEARTBEAT: Final = "heartbeat"

STATUS_STARTED: Final = "started"
STATUS_PROGRESS: Final = "progress"
STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_BLOCKED: Final = "blocked"

STAGE_CREATED: Final = "created"
STAGE_CALIBRATING: Final = "calibrating"
STAGE_COMPLETE: Final = "complete"
STAGE_FAILED: Final = "failed"
STAGE_BLOCKED: Final = "blocked"

CALIBRATION_EVENT_KIND: Final = "calibration_epoch"
BUILD_STARTED_MESSAGE: Final = "Microcosm build started."
BUILD_COMPLETED_MESSAGE: Final = "Microcosm build completed."
BUILD_BLOCKED_MESSAGE: Final = "Validation checks refused the candidate at {phase}."
UNEXPECTED_PROCESS_EXIT_MESSAGE: Final = (
    "The build process exited without reporting completion."
)

MAX_TELEMETRY_TEXT_CHARS: Final = 2_000
MAX_TELEMETRY_MESSAGE_CHARS: Final = 500
MAX_TELEMETRY_DETAILS_DEPTH: Final = 6
MAX_TELEMETRY_COLLECTION_ITEMS: Final = 200
MAX_TELEMETRY_DETAILS_BYTES: Final = 8_192
MAX_TELEMETRY_CHECK_ID_CHARS: Final = 200
SEQUENTIAL_STATUS_MAP: Final = MappingProxyType(
    {
        "failed": STATUS_FAILED,
        "completed": STATUS_COMPLETED,
        "passed": STATUS_COMPLETED,
        "progress": STATUS_PROGRESS,
        "started": STATUS_STARTED,
        "running": STATUS_STARTED,
    }
)

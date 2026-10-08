"""Shared constants for local and hosted Microcosm telemetry messages."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal

type TelemetryEventType = Literal[
    "run", "stage", "progress", "calibration", "heartbeat"
]
type TelemetryStatus = Literal["started", "progress", "completed", "failed"]

TELEMETRY_SCHEMA_VERSION: Final = 1

EVENT_TYPE_RUN: Final = "run"
EVENT_TYPE_STAGE: Final = "stage"
EVENT_TYPE_PROGRESS: Final = "progress"
EVENT_TYPE_CALIBRATION: Final = "calibration"
EVENT_TYPE_HEARTBEAT: Final = "heartbeat"

STATUS_STARTED: Final = "started"
STATUS_PROGRESS: Final = "progress"
STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"

STAGE_CREATED: Final = "created"
STAGE_CALIBRATING: Final = "calibrating"
STAGE_COMPLETE: Final = "complete"
STAGE_FAILED: Final = "failed"

CALIBRATION_EVENT_KIND: Final = "calibration_epoch"
BUILD_STARTED_MESSAGE: Final = "Microcosm build started."
BUILD_COMPLETED_MESSAGE: Final = "Microcosm build completed."
UNEXPECTED_PROCESS_EXIT_MESSAGE: Final = (
    "The build process exited without reporting completion."
)

MAX_TELEMETRY_TEXT_CHARS: Final = 2_000
MAX_TELEMETRY_MESSAGE_CHARS: Final = 500
MAX_TELEMETRY_DETAILS_DEPTH: Final = 6
MAX_TELEMETRY_COLLECTION_ITEMS: Final = 200
MAX_TELEMETRY_DETAILS_BYTES: Final = 8_192
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

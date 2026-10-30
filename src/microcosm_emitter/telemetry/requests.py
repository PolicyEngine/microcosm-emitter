"""Commands sent over the local socket, not the hosted collector's event API."""

from typing import Final

from microcosm_emitter_interface.telemetry import (
    DEFAULT_FAILURE_CLASS,
)

# Every public name, including those re-exported from the interface package,
# so type checkers of this py.typed package treat them as exported.
__all__ = [
    "COMMAND_BLOCK",
    "COMMAND_CALIBRATION_PROGRESS",
    "COMMAND_COMPLETE",
    "COMMAND_EMIT",
    "COMMAND_FAIL",
    "COMMAND_PROGRESS",
    "COMMAND_STAGE",
    "COMMAND_TRANSITION_CALIBRATION_PROGRESS",
    "COMMAND_TRANSITION_STAGE",
    "DEFAULT_FAILURE_CLASS",
]

COMMAND_EMIT: Final = "emit"
COMMAND_STAGE: Final = "stage"
COMMAND_TRANSITION_STAGE: Final = "transition_stage"
COMMAND_PROGRESS: Final = "progress"
COMMAND_CALIBRATION_PROGRESS: Final = "calibration_progress"
COMMAND_TRANSITION_CALIBRATION_PROGRESS: Final = "transition_calibration_progress"
COMMAND_FAIL: Final = "fail"
COMMAND_BLOCK: Final = "block"
COMMAND_COMPLETE: Final = "complete"

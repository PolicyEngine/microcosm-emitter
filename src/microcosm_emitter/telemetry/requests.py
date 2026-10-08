"""Commands sent over the local socket, not the hosted collector's event API."""

from typing import Final

COMMAND_EMIT: Final = "emit"
COMMAND_STAGE: Final = "stage"
COMMAND_TRANSITION_STAGE: Final = "transition_stage"
COMMAND_PROGRESS: Final = "progress"
COMMAND_CALIBRATION_PROGRESS: Final = "calibration_progress"
COMMAND_TRANSITION_CALIBRATION_PROGRESS: Final = "transition_calibration_progress"
COMMAND_FAIL: Final = "fail"
COMMAND_COMPLETE: Final = "complete"
DEFAULT_FAILURE_CLASS: Final = "build_failure"

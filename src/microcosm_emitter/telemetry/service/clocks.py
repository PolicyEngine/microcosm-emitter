"""Clocks that tell awake time from time asleep, and the sleeps between ticks."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime

SLEEP_THRESHOLD_SECONDS: Final = 5.0


@dataclass(frozen=True)
class ClockReading:
    """Two clock readings and the wall-clock time, taken together."""

    awake: float
    total: float
    wall: datetime


@dataclass(frozen=True)
class Sleep:
    """One system sleep: when it started and ended, and its length in seconds."""

    started_at: datetime
    ended_at: datetime
    seconds: float

    def as_details(self) -> dict[str, object]:
        """Not yet implemented."""
        return {}


def clock_ids(platform: str) -> tuple[int, int] | None:  # noqa: ARG001
    """Not yet implemented."""
    return None


class SystemClocks:
    """Not yet implemented."""

    def __init__(self, platform: str = sys.platform) -> None:
        """Not yet implemented."""
        self.platform = platform

    def read(self) -> ClockReading | None:
        """Not yet implemented."""
        return None


class SleepDetector:
    """Not yet implemented."""

    def __init__(self, threshold_seconds: float = SLEEP_THRESHOLD_SECONDS) -> None:
        """Not yet implemented."""
        self.threshold_seconds = threshold_seconds

    def observe(self, reading: ClockReading | None) -> Sleep | None:  # noqa: ARG002
        """Not yet implemented."""
        return None

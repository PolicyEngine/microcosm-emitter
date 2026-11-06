"""Clocks that tell awake time from time asleep, and the sleeps between ticks.

Each supported platform has a clock that stops while the machine sleeps and one
that keeps running: on macOS ``CLOCK_UPTIME_RAW`` (which ``time.monotonic()``
reads) and ``CLOCK_MONOTONIC``, and on Linux ``CLOCK_MONOTONIC`` and
``CLOCK_BOOTTIME``. Between two readings, the second clock's advance minus the
first's is the time the machine slept. Neither clock is stepped by time
synchronisation, unlike the wall clock, which is read only to date a sleep.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

#: A difference between the clocks of at least this many seconds is a sleep; a
#: smaller one is the delay of a tick that ran late.
SLEEP_THRESHOLD_SECONDS: Final = 5.0

_CLOCK_NAMES: Final = {
    "darwin": ("CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC"),
    "linux": ("CLOCK_MONOTONIC", "CLOCK_BOOTTIME"),
}


@dataclass(frozen=True)
class ClockReading:
    """Two clock readings and the wall-clock time, taken together.

    ``awake`` stops while the machine sleeps and ``total`` keeps running; both
    are seconds from an arbitrary origin. ``wall`` is the time in UTC.
    """

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
        """Return the sleep as a heartbeat's ``details.sleep``."""
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "seconds": round(self.seconds, 3),
        }


def clock_ids(platform: str) -> tuple[int, int] | None:
    """Return the clock that stops during sleep and the one that keeps running.

    ``platform`` is a ``sys.platform`` value. Any platform but macOS and Linux,
    or a Python without either clock, has none.
    """
    family = "linux" if platform.startswith("linux") else platform
    names = _CLOCK_NAMES.get(family)
    if names is None:
        return None
    awake, total = (getattr(time, name, None) for name in names)
    if awake is None or total is None:
        return None
    return awake, total


class SystemClocks:
    """Read this platform's two clocks and the wall clock together."""

    def __init__(self, platform: str = sys.platform) -> None:
        """Choose the clocks for ``platform``; without them every read is None."""
        self._ids = clock_ids(platform)

    def read(self) -> ClockReading | None:
        """Return the current readings, or None on a platform without the clocks."""
        if self._ids is None:
            return None
        awake, total = self._ids
        return ClockReading(
            time.clock_gettime(awake), time.clock_gettime(total), datetime.now(UTC)
        )


class SleepDetector:
    """Find a sleep between each clock reading and the one before it."""

    def __init__(self, threshold_seconds: float = SLEEP_THRESHOLD_SECONDS) -> None:
        """Count a difference of at least ``threshold_seconds`` as a sleep."""
        self.threshold_seconds = threshold_seconds
        self._previous: ClockReading | None = None

    def observe(self, reading: ClockReading | None) -> Sleep | None:
        """Return the sleep since the previous reading, if the machine slept.

        The sleep is dated from the previous reading's wall-clock time plus half
        the awake time between the two readings, which places it within one
        tick of when it happened. A missing reading, on either side, finds none.
        """
        previous, self._previous = self._previous, reading
        if previous is None or reading is None:
            return None
        awake = reading.awake - previous.awake
        asleep = (reading.total - previous.total) - awake
        if asleep < self.threshold_seconds:
            return None
        started_at = previous.wall + timedelta(seconds=max(0.0, awake) / 2)
        return Sleep(started_at, started_at + timedelta(seconds=asleep), asleep)

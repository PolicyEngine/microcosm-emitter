"""Heartbeat readings: the machine's memory, swap, pressure, CPU, power and disk."""

from __future__ import annotations

import sys
from typing import Any


class MachineReader:
    """Not yet implemented."""

    def __init__(self, parent_pid: int, *, system_platform: str = sys.platform) -> None:
        """Not yet implemented."""
        self.parent_pid = parent_pid
        self.system_platform = system_platform

    def reading(self) -> dict[str, Any]:
        """Not yet implemented."""
        return {}

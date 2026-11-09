"""Heartbeat readings: the machine's memory, swap, pressure, CPU, power and disk.

Every heartbeat carries one reading as ``details.reading``. Values that can't be
read on a platform are None: memory pressure as a level and compressed memory
only on macOS; stall shares, swap traffic and cgroup memory only on Linux.
macOS's page-in and page-out counters, which psutil reports as swap traffic,
count every kind of paging, so they are not reported.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import psutil

from microcosm_emitter.telemetry.service.lifecycle import current_stage
from microcosm_emitter.telemetry.service.system_readers import (
    MEMORY_PRESSURE_LEVELS,
    PSUTIL_READ_ERRORS,
    CgroupReadings,
    cgroup_directory,
    read_cgroup,
    read_pressure,
    sysctl_number,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from microcosm_emitter.telemetry.service.lifecycle import LifecycleState

_SHARE_DIGITS: Final = 4
_LOAD_DIGITS: Final = 2
_SECONDS_DIGITS: Final = 3
_IDLE_FIELDS: Final = frozenset({"idle", "iowait"})
# Linux counts guest time inside user and nice time as well.
_REPEATED_FIELDS: Final = frozenset({"guest", "guest_nice"})


class _Counters(Protocol):
    """A psutil named tuple of counters, such as ``psutil.cpu_times()``'s."""

    @property
    def _fields(self) -> tuple[str, ...]:
        """The counters' names."""
        ...


class _SwapCounters(Protocol):
    """The swap counters ``psutil.swap_memory()`` returns."""

    @property
    def used(self) -> int:
        """Bytes of swap in use."""
        ...

    @property
    def total(self) -> int:
        """Bytes of swap in all."""
        ...

    @property
    def sin(self) -> int:
        """Bytes swapped in since the system started."""
        ...

    @property
    def sout(self) -> int:
        """Bytes swapped out since the system started."""
        ...


def _cpu_times() -> _Counters | None:
    try:
        return psutil.cpu_times()
    except PSUTIL_READ_ERRORS:
        return None


def _busy_share(previous: _Counters | None, current: _Counters | None) -> float | None:
    """Return the share of all CPUs' time not idle between two readings."""
    if previous is None or current is None:
        return None
    total = 0.0
    idle = 0.0
    for field in current._fields:
        if field in _REPEATED_FIELDS:
            continue
        change = float(getattr(current, field)) - float(getattr(previous, field, 0.0))
        total += change
        if field in _IDLE_FIELDS:
            idle += change
    if total <= 0:
        return None
    return round(min(1.0, max(0.0, 1 - idle / total)), _SHARE_DIGITS)


def _swap_memory() -> _SwapCounters | None:
    try:
        return psutil.swap_memory()
    except PSUTIL_READ_ERRORS:
        return None


def _available_memory_bytes() -> int | None:
    try:
        return int(psutil.virtual_memory().available)
    except PSUTIL_READ_ERRORS:
        return None


def _load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], _LOAD_DIGITS)
    except OSError:
        return None


def _on_battery() -> bool | None:
    """Whether the machine runs on battery power; None without a battery."""
    read_battery = getattr(psutil, "sensors_battery", None)
    if read_battery is None:
        return None
    try:
        battery = read_battery()
    except PSUTIL_READ_ERRORS:
        return None
    if battery is None or battery.power_plugged is None:
        return None
    return not battery.power_plugged


def _disk_free_bytes(pid: int) -> int | None:
    """Return the free space on the volume of the build's working directory."""
    try:
        directory = psutil.Process(pid).cwd()
    except PSUTIL_READ_ERRORS:
        return None
    try:
        return int(shutil.disk_usage(directory).free)
    except OSError:
        return None


class MachineReader:
    """Read the machine's memory, swap, pressure, CPU, power and disk."""

    def __init__(
        self,
        parent_pid: int,
        *,
        system_platform: str = sys.platform,
        environ: Mapping[str, str] = os.environ,  # noqa: ARG002
        vmstat: Path = Path("/proc/vmstat"),  # noqa: ARG002
    ) -> None:
        """Hold the build's process ID and take the first CPU and swap counters."""
        self.parent_pid = parent_pid
        self._macos = system_platform == "darwin"
        self._linux = system_platform.startswith("linux")
        self._cgroup = cgroup_directory() if self._linux else None
        self._cpu = _cpu_times()
        swap = _swap_memory() if self._linux else None
        self._swap = None if swap is None else (int(swap.sin), int(swap.sout))

    def reading(self) -> dict[str, Any]:
        """Return the machine's part of a reading.

        The CPU share and the swap traffic cover the time since the previous
        reading, or since this reader was made.
        """
        swap = _swap_memory()
        cgroup = read_cgroup(self._cgroup) if self._linux else CgroupReadings()
        some = cgroup.memory_stall_some_share
        full = cgroup.memory_stall_full_share
        if self._linux and some is None:
            some, full = read_pressure()
        cpu = _cpu_times()
        busy = _busy_share(self._cpu, cpu)
        self._cpu = cpu
        swap_in, swap_out = self._swap_change(swap)
        level = sysctl_number("kern.memorystatus_vm_pressure_level")
        return {
            "memory_available_bytes": _available_memory_bytes(),
            "memory_compressed_bytes": (
                sysctl_number("vm.compressor_bytes_used") if self._macos else None
            ),
            "memory_pressure_level": (
                MEMORY_PRESSURE_LEVELS.get(level)
                if self._macos and level is not None
                else None
            ),
            "memory_stall_some_share": some,
            "memory_stall_full_share": full,
            "swap_used_bytes": None if swap is None else int(swap.used),
            "swap_total_bytes": None if swap is None else int(swap.total),
            "swap_in_bytes": swap_in,
            "swap_out_bytes": swap_out,
            "cpu_busy_share": busy,
            "load_average_1m": _load_average(),
            "on_battery": _on_battery(),
            "disk_free_bytes": _disk_free_bytes(self.parent_pid),
            "cgroup_memory_bytes": cgroup.memory_bytes,
            "cgroup_memory_limit_bytes": cgroup.memory_limit_bytes,
        }

    def _swap_change(self, swap: _SwapCounters | None) -> tuple[int | None, int | None]:
        """Return the bytes swapped in and out since the previous reading (Linux)."""
        if not self._linux or swap is None:
            return None, None
        counters = (int(swap.sin), int(swap.sout))
        previous, self._swap = self._swap, counters
        if previous is None:
            return None, None
        return max(0, counters[0] - previous[0]), max(0, counters[1] - previous[1])


def reading_details(
    state: LifecycleState,
    *,
    interval_seconds: float,
    late_seconds: float,
    process_tree: dict[str, Any],
    machine: dict[str, Any],
) -> dict[str, Any]:
    """Return a heartbeat's ``details.reading``.

    ``progress`` is the latest progress the build reported, by any request,
    while its stage is the heartbeat's stage or still open, or while the stage
    that was innermost when it was reported is still open; otherwise None.
    """
    open_stage_ids = [stage.stage_id for stage in state.open_stages]
    open_stages = {(stage.stage_id, stage.occurrence) for stage in state.open_stages}
    progress = state.latest_progress
    current = {*open_stage_ids, current_stage(state)}
    return {
        "interval_seconds": round(interval_seconds, _SECONDS_DIGITS),
        "late_seconds": round(late_seconds, _SECONDS_DIGITS),
        "stages": open_stage_ids,
        "progress": (
            progress.as_reading()
            if progress is not None
            and (progress.stage_id in current or progress.within in open_stages)
            else None
        ),
        "process_tree": process_tree,
        "machine": machine,
    }

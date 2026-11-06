"""Process-tree resource sampling for telemetry events and heartbeat readings."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Final, Protocol

import psutil

from microcosm_emitter.telemetry.service.system_readers import macos_process_usage

_CORES_DIGITS: Final = 4


class _CpuTimes(Protocol):
    """A process's CPU times, as ``psutil.Process.cpu_times()`` returns them."""

    @property
    def user(self) -> float:
        """Seconds spent in user mode."""
        ...

    @property
    def system(self) -> float:
        """Seconds spent in the kernel."""
        ...


@dataclass(frozen=True)
class _ProcessTotals:
    """Cumulative CPU seconds and disk bytes of one process, or of several."""

    user: float = 0.0
    system: float = 0.0
    disk_read: int = 0
    disk_written: int = 0

    def __add__(self, other: _ProcessTotals) -> _ProcessTotals:
        """Return the sum of both totals."""
        return _ProcessTotals(
            self.user + other.user,
            self.system + other.system,
            self.disk_read + other.disk_read,
            self.disk_written + other.disk_written,
        )

    def __sub__(self, other: _ProcessTotals) -> _ProcessTotals:
        """Return how much these totals exceed ``other``'s."""
        return _ProcessTotals(
            self.user - other.user,
            self.system - other.system,
            self.disk_read - other.disk_read,
            self.disk_written - other.disk_written,
        )


@dataclass(frozen=True)
class _ProcessSample:
    """One process's reading at one sample."""

    totals: _ProcessTotals
    memory_bytes: int
    disk_readable: bool


def _still_running(key: tuple[int, float]) -> bool:
    """Whether the process with this ID and start time still runs."""
    pid, started = key
    try:
        return psutil.Process(pid).create_time() == started
    except (psutil.Error, OSError):
        return False


class ProcessTreeSampler:
    """Sample a build's process tree: the build and every descendant.

    ``sample`` returns the cumulative CPU and resident memory that every event
    carries as ``resources``. It also keeps what a heartbeat's reading reports:
    the tree's memory (macOS's physical footprint, which counts compressed
    pages, or RSS elsewhere and for a process whose footprint can't be read)
    and its peaks, and each process's own CPU and disk totals. A process that
    exits keeps its last totals, so the tree's CPU and disk totals never fall;
    one that starts and exits between two samples is not counted.
    """

    def __init__(self, parent_pid: int, *, system_platform: str = sys.platform) -> None:
        """Hold the build's process ID; every total and peak starts at zero."""
        self.parent_pid = parent_pid
        self._peak_rss = 0
        self._macos = system_platform == "darwin"
        self._processes = 0
        self._memory = 0
        self._memory_peak = 0
        self._interval_memory_peak = 0
        self._disk_readable = False
        self._live: dict[tuple[int, float], _ProcessTotals] = {}
        self._exited = _ProcessTotals()
        self._reported: _ProcessTotals | None = None

    def sample(self) -> dict[str, Any]:
        """Sample the parent and every currently visible child process."""
        processes = []
        try:
            parent = psutil.Process(self.parent_pid)
            processes = [parent, *parent.children(recursive=True)]
        except (psutil.Error, OSError):
            pass
        user = 0.0
        system = 0.0
        rss = 0
        samples: dict[tuple[int, float], _ProcessSample] = {}
        for process in processes:
            try:
                with process.oneshot():
                    cpu = process.cpu_times()
                    memory = process.memory_info()
                    user += float(cpu.user) + float(getattr(cpu, "children_user", 0.0))
                    system += float(cpu.system) + float(
                        getattr(cpu, "children_system", 0.0)
                    )
                    rss += int(memory.rss)
                    samples[(process.pid, process.create_time())] = self._process(
                        process, cpu, int(memory.rss)
                    )
            except (psutil.Error, OSError):
                continue
        self._record(samples)
        self._peak_rss = max(self._peak_rss, rss)
        return {
            "cpu_user_seconds": user,
            "cpu_system_seconds": system,
            "rss_bytes": rss,
            "peak_rss_bytes": self._peak_rss,
        }

    def _process(
        self, process: psutil.Process, cpu: _CpuTimes, rss: int
    ) -> _ProcessSample:
        """Return one process's own CPU and disk totals and its memory."""
        user, system = float(cpu.user), float(cpu.system)
        if self._macos:
            usage = macos_process_usage(process.pid)
            if usage is not None:
                return _ProcessSample(
                    _ProcessTotals(
                        user, system, usage.disk_read_bytes, usage.disk_written_bytes
                    ),
                    usage.memory_bytes,
                    disk_readable=True,
                )
            return _ProcessSample(
                _ProcessTotals(user, system), rss, disk_readable=False
            )
        try:
            io = process.io_counters()
        except (psutil.Error, OSError, AttributeError):
            return _ProcessSample(
                _ProcessTotals(user, system), rss, disk_readable=False
            )
        return _ProcessSample(
            _ProcessTotals(user, system, int(io.read_bytes), int(io.write_bytes)),
            rss,
            disk_readable=True,
        )

    def _record(self, samples: dict[tuple[int, float], _ProcessSample]) -> None:
        """Adopt a sample's processes, keeping the totals of those that exited."""
        live = {key: sample.totals for key, sample in samples.items()}
        for key, totals in self._live.items():
            if key in live:
                continue
            if _still_running(key):
                # Running but not read this time: keep its last totals.
                live[key] = totals
            else:
                self._exited += totals
        self._live = live
        self._processes = len(samples)
        self._memory = sum(sample.memory_bytes for sample in samples.values())
        self._memory_peak = max(self._memory_peak, self._memory)
        self._interval_memory_peak = max(self._interval_memory_peak, self._memory)
        self._disk_readable = any(sample.disk_readable for sample in samples.values())
        if self._reported is None:
            self._reported = self._totals()

    def _totals(self) -> _ProcessTotals:
        totals = self._exited
        for process_totals in self._live.values():
            totals += process_totals
        return totals

    def tree_reading(self, interval_seconds: float) -> dict[str, Any]:
        """Return the tree's part of a reading, then start the next interval.

        The CPU shares are the cores in use since the previous reading (or the
        first sample), over ``interval_seconds`` of awake time. The disk bytes
        are those read and written since then, or None when no process's could
        be read.
        """
        totals = self._totals()
        change = totals - (self._reported or totals)
        self._reported = totals
        reading = {
            "processes": self._processes,
            "memory_bytes": self._memory,
            "memory_peak_bytes": self._memory_peak,
            "interval_memory_peak_bytes": max(self._interval_memory_peak, self._memory),
            "cpu_user_cores": _cores(change.user, interval_seconds),
            "cpu_system_cores": _cores(change.system, interval_seconds),
            "disk_read_bytes": max(0, change.disk_read)
            if self._disk_readable
            else None,
            "disk_written_bytes": (
                max(0, change.disk_written) if self._disk_readable else None
            ),
        }
        self._interval_memory_peak = 0
        return reading


def _cores(seconds: float, interval_seconds: float) -> float | None:
    """Return CPU ``seconds`` over ``interval_seconds`` as cores, never below 0."""
    if interval_seconds <= 0:
        return None
    return round(max(0.0, seconds) / interval_seconds, _CORES_DIGITS)

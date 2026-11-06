"""Operating-system readings psutil does not provide."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessUsage:
    """One process's physical footprint and disk bytes, from macOS."""

    memory_bytes: int
    disk_read_bytes: int
    disk_written_bytes: int


@dataclass(frozen=True)
class CgroupReadings:
    """A Linux cgroup's memory, limits and memory pressure; None when unknown."""

    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    cpu_limit_cores: float | None = None
    memory_stall_some_share: float | None = None
    memory_stall_full_share: float | None = None


def sysctl_number(name: str) -> int | None:  # noqa: ARG001
    """Not yet implemented."""
    return None


def sysctl_text(name: str) -> str | None:  # noqa: ARG001
    """Not yet implemented."""
    return None


def macos_process_usage(pid: int) -> ProcessUsage | None:  # noqa: ARG001
    """Not yet implemented."""
    return None


def parse_pressure(text: str) -> tuple[float | None, float | None]:  # noqa: ARG001
    """Not yet implemented."""
    return None, None


def parse_cpu_max(text: str) -> float | None:  # noqa: ARG001
    """Not yet implemented."""
    return -1.0


def parse_memory_max(text: str) -> int | None:  # noqa: ARG001
    """Not yet implemented."""
    return -1


def cgroup_directory(
    membership: Path = Path("/proc/self/cgroup"),  # noqa: ARG001
    root: Path = Path("/sys/fs/cgroup"),  # noqa: ARG001
) -> Path | None:
    """Not yet implemented."""
    return None


def read_cgroup(directory: Path | None) -> CgroupReadings:  # noqa: ARG001
    """Not yet implemented."""
    return CgroupReadings(memory_bytes=-1)


def read_pressure(
    path: Path = Path("/proc/pressure/memory"),  # noqa: ARG001
) -> tuple[float | None, float | None]:
    """Not yet implemented."""
    return None, None

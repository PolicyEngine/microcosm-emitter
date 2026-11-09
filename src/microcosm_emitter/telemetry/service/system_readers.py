"""Operating-system readings psutil does not provide.

On macOS: a process's physical footprint and disk bytes, from
``proc_pid_rusage``, which reads any process of the same user (psutil's unique
memory needs more privilege for a child process), and number and text sysctls
such as the memory pressure level. On Linux: a process's cgroup v2 directory
and its memory, limits and memory pressure, and the system's memory pressure.
Every reader returns None, or readings of None, when its value can't be read.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

#: What a psutil read can raise when a value can't be read, besides psutil's own
#: errors: an operating-system error, and in psutil 7.2.2 a ValueError or
#: RuntimeError for a missing or malformed system file (such as gVisor's
#: per-process I/O file before 2026-03-19) or a failed macOS system query, and
#: NotImplementedError where a platform lacks the reading. A reader that gets
#: one reads the value as unknown, never fails the module.
PSUTIL_READ_ERRORS: Final = (
    psutil.Error,
    OSError,
    RuntimeError,
    ValueError,
    NotImplementedError,
)
#: ``kern.memorystatus_vm_pressure_level``'s values on macOS.
MEMORY_PRESSURE_LEVELS: Final = {1: "normal", 2: "warning", 4: "critical"}
_RUSAGE_INFO_V2: Final = 2
_UNLIMITED: Final = "max"
_PERCENT: Final = 100
_CPU_MAX_FIELDS: Final = 2
_UNIFIED_HIERARCHY_PREFIX: Final = "0::"


class _RusageInfoV2(ctypes.Structure):
    """``struct rusage_info_v2`` from macOS's ``<sys/resource.h>``."""

    _fields_ = (
        ("ri_uuid", ctypes.c_uint8 * 16),
        *(
            (name, ctypes.c_uint64)
            for name in (
                "ri_user_time",
                "ri_system_time",
                "ri_pkg_idle_wkups",
                "ri_interrupt_wkups",
                "ri_pageins",
                "ri_wired_size",
                "ri_resident_size",
                "ri_phys_footprint",
                "ri_proc_start_abstime",
                "ri_proc_exit_abstime",
                "ri_child_user_time",
                "ri_child_system_time",
                "ri_child_pkg_idle_wkups",
                "ri_child_interrupt_wkups",
                "ri_child_pageins",
                "ri_child_elapsed_abstime",
                "ri_diskio_bytesread",
                "ri_diskio_byteswritten",
            )
        ),
    )


@dataclass(frozen=True)
class ProcessUsage:
    """One process's physical footprint and cumulative disk bytes, from macOS."""

    memory_bytes: int
    disk_read_bytes: int
    disk_written_bytes: int


@dataclass(frozen=True)
class CgroupReadings:
    """A Linux cgroup's memory, limits and memory pressure; None when unknown.

    The stall shares are the fractions of the last 10 seconds in which some, or
    all, of the cgroup's tasks waited for memory.
    """

    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    cpu_limit_cores: float | None = None
    memory_stall_some_share: float | None = None
    memory_stall_full_share: float | None = None


@functools.cache
def _macos_library() -> ctypes.CDLL | None:
    """Return macOS's C library with the two calls declared, or None elsewhere."""
    if sys.platform != "darwin":
        return None
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    try:
        library = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    library.sysctlbyname.argtypes = (
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    library.sysctlbyname.restype = ctypes.c_int
    library.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    library.proc_pid_rusage.restype = ctypes.c_int
    return library


def sysctl_number(name: str) -> int | None:
    """Return a macOS sysctl that holds an unsigned number of up to 8 bytes."""
    library = _macos_library()
    if library is None:
        return None
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if library.sysctlbyname(name.encode(), ctypes.byref(value), size, None, 0) != 0:
        return None
    # A 4-byte value fills the low half of the zeroed 8 bytes on the
    # little-endian processors macOS runs on.
    return int(value.value)


def sysctl_text(name: str) -> str | None:
    """Return a macOS sysctl that holds text, or None when it is empty."""
    library = _macos_library()
    if library is None:
        return None
    size = ctypes.c_size_t(0)
    encoded = name.encode()
    if library.sysctlbyname(encoded, None, size, None, 0) != 0 or size.value == 0:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if library.sysctlbyname(encoded, buffer, size, None, 0) != 0:
        return None
    return buffer.value.decode(errors="replace").strip() or None


def macos_process_usage(pid: int) -> ProcessUsage | None:
    """Return a process's physical footprint and disk bytes, on macOS only."""
    library = _macos_library()
    if library is None:
        return None
    info = _RusageInfoV2()
    if library.proc_pid_rusage(pid, _RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return ProcessUsage(
        int(info.ri_phys_footprint),
        int(info.ri_diskio_bytesread),
        int(info.ri_diskio_byteswritten),
    )


def parse_pressure(text: str) -> tuple[float | None, float | None]:
    """Return the ``some`` and ``full`` 10-second averages of a pressure file.

    Each is a fraction (the file gives a percentage), or None when absent.
    """
    shares: dict[str, float | None] = {"some": None, "full": None}
    for row in text.splitlines():
        kind, _, fields = row.partition(" ")
        if kind not in shares:
            continue
        for field in fields.split():
            key, _, value = field.partition("=")
            if key == "avg10":
                with contextlib.suppress(ValueError):
                    shares[kind] = float(value) / _PERCENT
    return shares["some"], shares["full"]


def parse_cpu_max(text: str) -> float | None:
    """Return a ``cpu.max`` limit in cores, or None when unlimited or unreadable."""
    fields = text.split()
    if len(fields) != _CPU_MAX_FIELDS or fields[0] == _UNLIMITED:
        return None
    try:
        quota, period = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    return quota / period if period > 0 else None


def parse_memory_max(text: str) -> int | None:
    """Return a ``memory.max`` limit in bytes, or None when unlimited or unreadable."""
    return None if text.strip() == _UNLIMITED else _parse_bytes(text)


def _parse_bytes(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def cgroup_directory(
    membership: Path = Path("/proc/self/cgroup"),
    root: Path = Path("/sys/fs/cgroup"),
) -> Path | None:
    """Return this process's cgroup v2 directory, or None without one.

    ``membership`` lists the process's cgroups; the unified hierarchy's entry
    starts ``0::``. The directory must hold ``cgroup.controllers``, as every
    cgroup v2 directory does.
    """
    text = _read_text(membership)
    if text is None:
        return None
    for row in text.splitlines():
        if row.startswith(_UNIFIED_HIERARCHY_PREFIX):
            relative = row.removeprefix(_UNIFIED_HIERARCHY_PREFIX).strip().lstrip("/")
            directory = root / relative
            return directory if (directory / "cgroup.controllers").is_file() else None
    return None


def read_cgroup(directory: Path | None) -> CgroupReadings:
    """Read a cgroup's memory, its memory and CPU limits and its memory pressure."""
    if directory is None:
        return CgroupReadings()
    current = _read_text(directory / "memory.current")
    memory_max = _read_text(directory / "memory.max")
    cpu_max = _read_text(directory / "cpu.max")
    pressure = _read_text(directory / "memory.pressure")
    some, full = parse_pressure(pressure) if pressure is not None else (None, None)
    return CgroupReadings(
        memory_bytes=_parse_bytes(current) if current is not None else None,
        memory_limit_bytes=(
            parse_memory_max(memory_max) if memory_max is not None else None
        ),
        cpu_limit_cores=parse_cpu_max(cpu_max) if cpu_max is not None else None,
        memory_stall_some_share=some,
        memory_stall_full_share=full,
    )


def read_pressure(
    path: Path = Path("/proc/pressure/memory"),
) -> tuple[float | None, float | None]:
    """Return the system's memory stall shares, or None for each when unreadable."""
    text = _read_text(path)
    return parse_pressure(text) if text is not None else (None, None)

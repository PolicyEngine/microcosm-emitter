"""The description of the machine a build runs on, without naming it.

The run-started event carries it as ``details.environment``: whether the build
runs on a local machine or in a Modal container, the operating system, the
processor and the memory, and the emitter and Python versions. It never holds
a hostname, user name, network address, serial number or file path, and of
Modal's container variables it reads only the task ID, region and cloud
provider (never ``MODAL_IDENTITY_TOKEN``, a credential).
"""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psutil

from microcosm_emitter.telemetry.service.sanitization import sanitize_text
from microcosm_emitter.telemetry.service.system_readers import (
    PSUTIL_READ_ERRORS,
    CgroupReadings,
    cgroup_directory,
    read_cgroup,
    sysctl_text,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

KIND_LOCAL: Final = "local"
KIND_MODAL_CONTAINER: Final = "modal_container"
#: Set in every Modal container: the container's ID, its region and its cloud.
MODAL_TASK_VARIABLE: Final = "MODAL_TASK_ID"
MODAL_REGION_VARIABLE: Final = "MODAL_REGION"
MODAL_CLOUD_PROVIDER_VARIABLE: Final = "MODAL_CLOUD_PROVIDER"
_DISTRIBUTION: Final = "microcosm-emitter"
_TEXT_LIMIT: Final = 200
_CPU_INFO: Final = Path("/proc/cpuinfo")


def _text(value: str | None) -> str | None:
    """Return ``value`` redacted, bounded and stripped, or None when blank."""
    if value is None or not value.strip():
        return None
    return sanitize_text(value.strip(), limit=_TEXT_LIMIT)


def _modal(environ: Mapping[str, str]) -> dict[str, str | None] | None:
    task_id = _text(environ.get(MODAL_TASK_VARIABLE))
    if task_id is None:
        return None
    return {
        "task_id": task_id,
        "region": _text(environ.get(MODAL_REGION_VARIABLE)),
        "cloud_provider": _text(environ.get(MODAL_CLOUD_PROVIDER_VARIABLE)),
    }


def _os_version(system_platform: str) -> str | None:
    if system_platform == "darwin":
        return _text(platform.mac_ver()[0])
    return _text(platform.release())


def _cpu_model(system_platform: str) -> str | None:
    if system_platform == "darwin":
        return _text(sysctl_text("machdep.cpu.brand_string"))
    try:
        text = _CPU_INFO.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for row in text.splitlines():
        key, _, value = row.partition(":")
        if key.strip() == "model name":
            return _text(value)
    return None


def _memory_total_bytes() -> int | None:
    try:
        return int(psutil.virtual_memory().total)
    except PSUTIL_READ_ERRORS:
        return None


def _emitter_version() -> str | None:
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def machine_environment(
    environ: Mapping[str, str] = os.environ,
    system_platform: str = sys.platform,
) -> dict[str, object]:
    """Describe the machine the build runs on.

    A container with ``MODAL_TASK_ID`` set is a Modal container; anything else
    is a local machine. Each value that can't be read is None.
    """
    modal = _modal(environ)
    cgroup = (
        read_cgroup(cgroup_directory())
        if system_platform.startswith("linux")
        else CgroupReadings()
    )
    return {
        "kind": KIND_MODAL_CONTAINER if modal is not None else KIND_LOCAL,
        "os": _text(platform.system()),
        "os_version": _os_version(system_platform),
        "architecture": _text(platform.machine()),
        "cpu_model": _cpu_model(system_platform),
        "logical_cpus": os.cpu_count(),
        "memory_total_bytes": _memory_total_bytes(),
        "cgroup_cpu_limit_cores": cgroup.cpu_limit_cores,
        "cgroup_memory_limit_bytes": cgroup.memory_limit_bytes,
        "modal": modal,
        "emitter_version": _emitter_version(),
        "python_version": platform.python_version(),
    }

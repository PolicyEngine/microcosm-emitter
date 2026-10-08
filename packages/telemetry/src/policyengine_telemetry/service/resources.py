"""Process-tree resource sampling for telemetry events."""

from __future__ import annotations

from typing import Any

import psutil


class ProcessTreeSampler:
    """Collect cumulative CPU and resident memory for a build process tree."""

    def __init__(self, parent_pid: int) -> None:
        self.parent_pid = parent_pid
        self._peak_rss = 0

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
        for process in processes:
            try:
                cpu = process.cpu_times()
                user += float(cpu.user) + float(getattr(cpu, "children_user", 0.0))
                system += float(cpu.system) + float(
                    getattr(cpu, "children_system", 0.0)
                )
                rss += int(process.memory_info().rss)
            except (psutil.Error, OSError):
                continue
        self._peak_rss = max(self._peak_rss, rss)
        return {
            "cpu_user_seconds": user,
            "cpu_system_seconds": system,
            "rss_bytes": rss,
            "peak_rss_bytes": self._peak_rss,
        }

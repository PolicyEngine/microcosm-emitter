"""Monitor a process identity, not just a reusable numeric PID."""

import psutil


class ParentProcess:
    def __init__(self, pid: int, created_at: float):
        self.pid = pid
        self.created_at = created_at

    def alive(self) -> bool:
        try:
            process = psutil.Process(self.pid)
            return (
                process.create_time() == self.created_at
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except (psutil.Error, OSError):
            return False

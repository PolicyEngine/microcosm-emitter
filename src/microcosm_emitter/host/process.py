"""Monitor a process identity, not just a reusable numeric PID."""

import os

import psutil


class ParentProcess:
    """The parent process, identified by its process ID and its creation time.

    A process later given the same ID has another creation time, so it does not
    count as the parent.
    """

    def __init__(self, pid: int, created_at: float) -> None:
        """Hold the process ID and the creation time, in seconds since the epoch."""
        self.pid = pid
        self.created_at = created_at

    def alive(self) -> bool:
        """Return whether the process with this ID and creation time still runs.

        A zombie process counts as exited, and so does any error reading the
        process.
        """
        try:
            process = psutil.Process(self.pid)
            return (
                process.create_time() == self.created_at
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except (psutil.Error, OSError):
            return False


def parent_process(pid: int, created_at: float | None) -> ParentProcess:
    """Identify the build that owns this host.

    A launcher may pass the creation time it measured. Otherwise the host reads
    the creation time of the process named ``pid`` and then checks that this
    process is still its parent. A parent that exits is replaced as parent by
    another, still running process, so a match after the read proves the
    creation time is the parent's, not that of an unrelated process that was
    given the same process ID after the parent exited.
    """
    if created_at is not None:
        return ParentProcess(pid, created_at)
    if os.getppid() != pid:
        error_message = "the named parent process is not this host's parent"
        raise ValueError(error_message)
    parent_created_at = psutil.Process(pid).create_time()
    if os.getppid() != pid:
        error_message = "the named parent process is not this host's parent"
        raise ValueError(error_message)
    return ParentProcess(pid, parent_created_at)

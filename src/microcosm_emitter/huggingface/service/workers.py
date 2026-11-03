"""Delivery workers: each kind of long upload on its own thread.

The host gives a module one worker thread. It calls ``tick`` once per tick and,
when the host stops, ``close``, which makes the commit at shutdown; ``close``
runs only after the current ``tick`` returns. So nothing that can take minutes
may run inside ``tick``. The Hugging Face module therefore commits staging run
files (small, at most one commit per repository) on the host's thread, and
gives each kind of upload that can take minutes a ``DeliveryWorker``: its own
thread, running that kind's delivery cycle once per interval.

A worker's upload then never delays run-file commits to another repository,
the commit at shutdown, or a read-back. Two writers never commit to one
repository at once: each delivery first leases its repository in the
hand-over database, and one that finds it leased tries again next cycle.

Once shutdown begins (``stop``), a worker starts no further upload: its
cycle receives ``stop_requested`` and checks it before each repository, once
that repository is leased, and once a job is claimed, immediately before the
upload begins; a job claimed after ``stop`` is given back unattempted. So
only an upload that passed that last check before ``stop`` was called can
still be running.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class DeliveryWorker:
    """Runs ``cycle`` on its own thread every ``interval_seconds`` until stopped.

    ``cycle`` is called with ``stop_requested``, which turns true once
    ``stop`` is called; a cycle checks it before it starts each upload. An
    exception from ``cycle`` is reported through ``on_error``; the work it
    was doing stays queued, and the next cycle tries again.
    """

    def __init__(
        self,
        name: str,
        cycle: Callable[[Callable[[], bool]], object],
        *,
        interval_seconds: float,
        on_error: Callable[[], None],
    ) -> None:
        """Set up the worker's thread, named after ``name``, without starting it."""
        self.name = name
        self.cycle = cycle
        self.interval_seconds = interval_seconds
        self.on_error = on_error
        self._stopping = threading.Event()
        # Held for the length of each cycle, so ``wait`` can tell a worker
        # that is uploading from one that is only waiting for its next cycle.
        self._in_cycle = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"microcosm-emitter-{name}", daemon=True
        )

    def start(self) -> None:
        """Start the worker's thread; its first cycle runs one interval later."""
        self._thread.start()

    def stop(self) -> None:
        """Start no further cycle or upload; an upload under way runs on."""
        self._stopping.set()

    def stop_requested(self) -> bool:
        """Whether ``stop`` was called: the cycle then starts no new upload."""
        return self._stopping.is_set()

    def wait(self, deadline: float) -> bool:
        """After ``stop``, wait for a cycle in progress until ``deadline``.

        ``deadline`` is a ``time.monotonic`` value. True means no cycle is
        running and none will start; False means one is still running, for
        example an upload that outlasts the shutdown period.
        """
        remaining = deadline - time.monotonic()
        acquired = (
            self._in_cycle.acquire(timeout=remaining)
            if remaining > 0
            else self._in_cycle.acquire(blocking=False)
        )
        if acquired:
            self._in_cycle.release()
        return acquired

    def _run(self) -> None:
        while not self._stopping.wait(self.interval_seconds):
            with self._in_cycle:
                if self._stopping.is_set():
                    return
                try:
                    self.cycle(self.stop_requested)
                except Exception:
                    self.on_error()

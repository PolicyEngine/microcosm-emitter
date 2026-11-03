"""Bounded FIFO request workers, independent of each module's periodic work."""

import contextlib
import socket
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from queue import Empty, Full, Queue


@dataclass(frozen=True)
class ModuleRequest:
    """One accepted connection whose acknowledgement follows its callback."""

    connection: socket.socket
    respond: Callable[[], bytes]


class ModuleRequestDispatcher:
    """Give each module one serial worker and a bounded pending-request queue."""

    def __init__(self, names: Iterable[str], *, max_pending_requests: int) -> None:
        """Create a queue and an unstarted worker thread for each module name.

        Each queue holds at most ``max_pending_requests`` requests.
        """
        self._queues: dict[str, Queue[ModuleRequest | None]] = {
            name: Queue(maxsize=max_pending_requests) for name in names
        }
        self._closed: set[str] = set()
        self._lock = threading.Lock()
        self._threads = {
            name: threading.Thread(target=self._run, args=(name,), daemon=True)
            for name in self._queues
        }

    def start(self) -> None:
        """Start every module's worker thread."""
        for thread in self._threads.values():
            thread.start()

    def submit(self, name: str, request: ModuleRequest) -> bool:
        """Reject overload or shutdown without claiming a callback completed."""
        with self._lock:
            if name in self._closed:
                return False
            try:
                self._queues[name].put_nowait(request)
            except Full:
                return False
        return True

    def stop_module(self, name: str) -> None:
        """Stop intake and wake the worker; accepted requests finish in order."""
        with self._lock:
            if name in self._closed:
                return
            self._closed.add(name)
            # When the queue is full, a busy worker sees closure after draining
            # its bounded queue.
            with contextlib.suppress(Full):
                self._queues[name].put_nowait(None)

    def stop(self) -> None:
        """Stop intake for every module; accepted requests still finish in order."""
        for name in self._queues:
            self.stop_module(name)

    def wait(self, name: str, *, timeout: float | None = None) -> bool:
        """Return only when this module has no callbacks or connections in use."""
        thread = self._threads[name]
        if thread.ident is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def wait_all(self) -> None:
        """Keep shared module resources open until every accepted callback finishes."""
        for name in self._threads:
            self.wait(name)

    def _run(self, name: str) -> None:
        requests = self._queues[name]
        while True:
            try:
                request = requests.get(timeout=0.05)
            except Empty:
                with self._lock:
                    if name in self._closed:
                        return
                continue
            if request is None:
                return
            with request.connection:
                response = request.respond()
                with contextlib.suppress(OSError):
                    request.connection.sendall(response)
            with self._lock:
                if name in self._closed and requests.empty():
                    return

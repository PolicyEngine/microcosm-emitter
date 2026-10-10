"""Advisory leases that tell a live producer's run from an orphaned one.

Every emitter service on a host shares one spool, so the spool also holds other
builds' runs. A run belongs to the service that produced it while that service
lives. The collector gives a run to the Hugging Face user who registers it
first, so another service delivering a live run would register it under its own
credential, and the collector's answer to that credential says nothing about
the run's own.

Each service therefore holds an exclusive ``flock`` on its producer's lease file
for its whole life. The kernel drops the lock when the process exits by any
route, SIGKILL included, so a lease that can be taken means the producing
service is gone and its run may be adopted. The lease files live beside the
spool, outside the database, so no spool migration is needed and checkouts that
predate leases can still share the spool.

Microcosm's telemetry service keeps the same lease files (directory, file name
and lock type) from PolicyEngine/microcosm#1177 on, so services from either
package recognise each other's leases.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the service needs Unix sockets anyway
    fcntl = None

from microcosm_emitter.telemetry.service.constants import (
    LEASE_DIRECTORY_SUFFIX,
    LEASE_FILE_SUFFIX,
    LEASE_RETRY_SECONDS,
    MAX_LEASE_OPEN_ATTEMPTS,
)


class OwnLeaseUnavailableError(RuntimeError):
    """A service could not take its own producer's lease."""


class ProducerLease:
    """An exclusive lock on one producer's lease file, held until released."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self._descriptor: int | None = descriptor

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def release(self, *, remove: bool = False) -> None:
        """Drop the lock, first unlinking the file when ``remove`` is set.

        The file is unlinked while the lock is still held, so no other process
        can be holding it then. Anyone who opened the old file meanwhile finds,
        once it gets the lock, that the path no longer names that file, and
        tries again.
        """

        if self._descriptor is None:
            return
        try:
            if remove:
                self.path.unlink(missing_ok=True)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> ProducerLease:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class ProducerLeases:
    """The lease files of every producer that shares one spool."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    @classmethod
    def beside(cls, spool_path: Path | str) -> ProducerLeases:
        """Return the leases for the spool at ``spool_path``."""

        spool_path = Path(spool_path)
        return cls(spool_path.with_name(spool_path.name + LEASE_DIRECTORY_SUFFIX))

    @property
    def supported(self) -> bool:
        """Whether this platform has advisory file locks."""

        return fcntl is not None

    def path(self, run_id: str, producer_id: str) -> Path:
        """Return the lease file of one producer.

        Identifiers come from the stored registration, so the file name is a
        digest of them rather than the identifiers themselves.
        """

        digest = hashlib.sha256(f"{run_id}\0{producer_id}".encode()).hexdigest()
        return self.directory / f"{digest}{LEASE_FILE_SUFFIX}"

    def exists(self, run_id: str, producer_id: str) -> bool:
        return self.path(run_id, producer_id).exists()

    def try_acquire(
        self,
        run_id: str,
        producer_id: str,
        *,
        create: bool,
    ) -> ProducerLease | None:
        """Take a producer's lease without waiting.

        Returns ``None`` while another process holds it. With ``create`` unset,
        a missing lease file raises ``FileNotFoundError`` rather than creating
        one, so the caller can tell a producer that never had a lease (an older
        checkout) from one that has gone. Any other failure to test the lease
        raises ``OSError``.
        """

        if fcntl is None:
            raise OSError("advisory file locks are not available")
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _lock(self.path(run_id, producer_id), create=create)

    def hold(
        self,
        run_id: str,
        producer_id: str,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ProducerLease | None:
        """Take this service's own lease, waiting out brief holders and errors.

        Only a sweeper or an adopter holds another producer's lease, and only
        for a moment, and a lock the system refuses once may be granted on the
        next attempt, so a short wait suffices. Returns ``None`` if the lease is
        still not held at the deadline, or on a platform without advisory
        locks. A failed attempt can leave a lease file nobody holds, which
        reads as an exited producer's, so the caller must not serve its run
        without the lease.
        """

        if fcntl is None:
            return None
        deadline = clock() + timeout_seconds
        while True:
            try:
                lease = self.try_acquire(run_id, producer_id, create=True)
            except OSError:
                lease = None
            if lease is not None or clock() >= deadline:
                return lease
            sleep(LEASE_RETRY_SECONDS)

    def sweep(self, keep: Iterable[tuple[str, str]]) -> int:
        """Remove free lease files of producers that have nothing pending.

        A held lease belongs to a live service and is never removed, nor is the
        lease of a run in ``keep`` (the pending runs), so an orphan stays
        adoptable at once. Returns the number of files removed.
        """

        if fcntl is None or not self.directory.is_dir():
            return 0
        kept = {self.path(run_id, producer_id).name for run_id, producer_id in keep}
        removed = 0
        for path in self.directory.glob(f"*{LEASE_FILE_SUFFIX}"):
            if path.name in kept:
                continue
            try:
                lease = _lock(path, create=False)
            except OSError:
                continue
            if lease is not None:
                lease.release(remove=True)
                removed += 1
        return removed


def _lock(path: Path, *, create: bool) -> ProducerLease | None:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    for _ in range(MAX_LEASE_OPEN_ATTEMPTS):
        descriptor = os.open(path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        try:
            current = _names_open_file(path, descriptor)
        except BaseException:
            # Never leave a lock held on a descriptor nobody will close.
            os.close(descriptor)
            raise
        if current:
            return ProducerLease(path, descriptor)
        # Another process removed this file between our open and our lock.
        os.close(descriptor)
        if not create and not path.exists():
            raise FileNotFoundError(path)
    # The path keeps being replaced under us: someone is taking it.
    return None


def _names_open_file(path: Path, descriptor: int) -> bool:
    try:
        on_disk = path.stat()
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)

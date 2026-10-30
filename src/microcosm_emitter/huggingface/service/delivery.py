"""Delivery cycles: at most one Hugging Face commit per repository per cycle.

Each cycle finds the repositories with work due, leases each one, and makes
one commit: a due job (a dataset bundle or a release change), or every
pending staging run file of that repository at once. The module runs the
run-file cycle on the host's thread and each job kind's cycle on its own
worker thread (see ``workers``), so a long upload holds only its own
repository. Failures keep the work queued and back off; nothing here raises
into the build.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from microcosm_emitter.huggingface.service.bundles import stage_bundle
from microcosm_emitter.huggingface.service.hub import UPLOAD_FAILED, DatasetRepository
from microcosm_emitter.huggingface.service.releases import (
    ReleaseOutcome,
    apply_release_change,
    expired_change,
    sets_current,
)
from microcosm_emitter.huggingface.service.requests import (
    RequestRefusedError,
    release_change_from_message,
)
from microcosm_emitter.huggingface.service.run_files import commit_run_files
from microcosm_emitter_interface.publication import RELEASE_CHANGE

if TYPE_CHECKING:
    from collections.abc import Callable

    from huggingface_hub import HfApi

    from microcosm_emitter.huggingface.service.queue import ClaimedJob, HandoverQueue

# Each print in this module that suppresses Ruff's T201 writes one of its
# messages to standard error on purpose; none is leftover debugging output.

#: How long a repository or job lease lasts without renewal. While a delivery
#: runs, its leases are renewed every ``LEASE_RENEWAL_SECONDS``, so a long
#: upload keeps them; an emitter that was killed stops renewing, and another
#: emitter on this machine takes the repository over within one lease.
REPOSITORY_LEASE_SECONDS = 120.0
LEASE_RENEWAL_SECONDS = 30.0
#: The job kinds this emitter delivers.
DATASET_BUNDLE = "dataset_bundle"
SUPPORTED_JOB_KINDS = frozenset({DATASET_BUNDLE, RELEASE_CHANGE})
#: Job kinds that skip the spacing between commits: release changes are rare
#: and a build waits for them. Bundles and run files keep it.
UNSPACED_JOB_KINDS = frozenset({RELEASE_CHANGE})
CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
CREDENTIAL_MISSING_MESSAGE = (
    "Microcosm Hugging Face uploads are paused: no Hugging Face credential was "
    "found (HF_TOKEN, HUGGINGFACE_TOKEN or `hf auth login`). Handed-over files "
    "stay queued; the build will continue."
)
UPLOAD_FAILED_MESSAGE = (
    "A Microcosm Hugging Face upload failed ({error_type}); handed-over files "
    "stay queued for a later attempt and the build will continue."
)


def never_stop() -> bool:
    """Return ``False``: the stop check of a cycle that no worker runs."""
    return False


def iso_timestamp(seconds: float) -> str:
    """Microcosm ``staging._now()`` format, at ``seconds``."""
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat()


class HeldLeases:
    """A repository lease, and the job lease once one is claimed, kept renewed.

    A thread renews both every ``renewal_seconds`` until ``stop``, or until
    ``stop_requested`` turns true: an attempt abandoned at shutdown then
    frees its leases within one lease. Read ``repository_lease`` and ``job``
    only after ``stop``: they then name the current leases, which releasing
    the repository and finishing the job must match. ``for_job`` holds a job
    without a repository lease, as a collector submission does.
    """

    def __init__(
        self,
        queue: HandoverQueue,
        repository: str | None,
        lease: float,
        *,
        clock: Callable[[], float],
        lease_seconds: float,
        renewal_seconds: float,
        stop_requested: Callable[[], bool] = never_stop,
    ) -> None:
        """Record the leases to renew; ``start`` runs the renewal thread.

        ``lease`` is when the repository lease ends. ``repository`` is ``None``
        when only a job is held (see ``for_job``).
        """
        self.queue, self.repository = queue, repository
        self.repository_lease = lease
        self.job: ClaimedJob | None = None
        self.clock = clock
        self.lease_seconds, self.renewal_seconds = lease_seconds, renewal_seconds
        self.stop_requested = stop_requested
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._renew_until_stopped, daemon=True)

    @classmethod
    def for_job(
        cls,
        queue: HandoverQueue,
        job: ClaimedJob,
        *,
        clock: Callable[[], float],
        lease_seconds: float,
        renewal_seconds: float,
        stop_requested: Callable[[], bool] = never_stop,
    ) -> HeldLeases:
        """``job``'s lease alone, with no repository lease."""
        held = cls(
            queue,
            None,
            0.0,
            clock=clock,
            lease_seconds=lease_seconds,
            renewal_seconds=renewal_seconds,
            stop_requested=stop_requested,
        )
        held.hold_job(job)
        return held

    def start(self) -> HeldLeases:
        """Start the renewal thread and return this object."""
        self._thread.start()
        return self

    def hold_job(self, job: ClaimedJob) -> None:
        """Hold ``job``'s lease too, so that later renewals extend it."""
        with self._lock:
            self.job = job

    def renew(self) -> None:
        """Extend the held leases to ``lease_seconds`` from now.

        A lease that already ends later is left as it is, and so is one the
        queue no longer matches because another writer has taken it over.
        """
        with self._lock:
            renewed = self.clock() + self.lease_seconds
            if (
                self.repository is not None
                and renewed > self.repository_lease
                and self.queue.renew_repository(
                    self.repository, self.repository_lease, renewed
                )
            ):
                self.repository_lease = renewed
            job = self.job
            if (
                job is not None
                and renewed > job.lease_until
                and self.queue.renew_job(job, renewed)
            ):
                self.job = replace(job, lease_until=renewed)

    def _renew_until_stopped(self) -> None:
        while not self._stopped.wait(self.renewal_seconds):
            if self.stop_requested():
                return
            # A failed renewal leaves the lease to expire; the delivery's
            # own result is recorded only if it still holds the lease.
            with contextlib.suppress(Exception):
                self.renew()

    def stop(self) -> None:
        """Stop renewing and wait for the renewal thread to finish."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join()


class HuggingFaceDelivery:
    """Commit queued hand-overs with the build's credential."""

    def __init__(
        self,
        queue: HandoverQueue,
        *,
        api_factory: Callable[[], Any | None],
        clock: Callable[[], float] = time.time,
        lease_renewal_seconds: float = LEASE_RENEWAL_SECONDS,
    ) -> None:
        """Hold the queue, the Hub client factory and the clock.

        ``api_factory`` returns a Hub client, or ``None`` when there is no
        credential.
        """
        self.queue = queue
        self.api_factory = api_factory
        self.clock = clock
        self.lease_renewal_seconds = lease_renewal_seconds
        self._warned: set[str] = set()
        self._warning_lock = threading.Lock()

    def warn_once(self, key: str, message: str) -> None:
        """Print ``message`` to standard error, once per ``key``."""
        with self._warning_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        print(message, file=sys.stderr, flush=True)  # noqa: T201

    def run_cycle(self) -> int:
        """Every kind of work, on the calling thread; return how many commits.

        A repository with a due job gets the job, and its run files wait for
        a later cycle. Used when nothing runs the cycles below on their own
        threads, for example a module driven by hand in tests.
        """
        return self._cycle(job_kinds=SUPPORTED_JOB_KINDS, run_files=True)

    def run_file_cycle(self) -> int:
        """Commit each due repository's pending run files; return how many."""
        return self._cycle(job_kinds=frozenset(), run_files=True)

    def job_cycle(
        self,
        kinds: frozenset[str],
        *,
        stop_requested: Callable[[], bool] = never_stop,
    ) -> int:
        """Deliver one due job of ``kinds`` per repository; return how many.

        ``stop_requested`` is checked before each repository, once its lease
        is taken, and once its job is claimed, immediately before the upload
        begins. Once it is true (shutdown has begun), the cycle starts no
        upload and returns; an upload that began before that continues.
        """
        return self._cycle(
            job_kinds=kinds & SUPPORTED_JOB_KINDS,
            run_files=False,
            stop_requested=stop_requested,
        )

    def _cycle(
        self,
        *,
        job_kinds: frozenset[str],
        run_files: bool,
        stop_requested: Callable[[], bool] = never_stop,
    ) -> int:
        repositories = self.queue.due_repositories(
            self.clock(),
            kinds=job_kinds,
            unspaced_kinds=UNSPACED_JOB_KINDS,
            run_files=run_files,
        )
        if not repositories:
            return 0
        api = self.api_factory()
        delivered = 0
        for repository in repositories:
            if stop_requested():
                break
            delivered += self._deliver_repository(
                repository,
                api,
                job_kinds=job_kinds,
                run_files=run_files,
                stop_requested=stop_requested,
            )
        return delivered

    def drain(self, deadline: float) -> None:
        """At shutdown, commit every repository's pending run files once.

        Ignores the commit interval and spacing, but still leases each
        repository: one whose lease a worker holds, during a long upload, is
        skipped. ``deadline`` is a ``time.monotonic`` value.
        """
        api = self.api_factory()
        for repository in self.queue.repositories_with_pending_run_files():
            if time.monotonic() >= deadline:
                return
            self._deliver_repository(
                repository, api, job_kinds=frozenset(), run_files=True, at_shutdown=True
            )

    def _deliver_repository(
        self,
        repository: str,
        api: HfApi | None,
        *,
        job_kinds: frozenset[str],
        run_files: bool,
        at_shutdown: bool = False,
        stop_requested: Callable[[], bool] = never_stop,
    ) -> int:
        """One commit to ``repository``: a job of ``job_kinds``, else run files.

        ``at_shutdown`` commits pending run files whether or not they are due.
        ``stop_requested`` is checked again once the repository is leased, and
        once more after a job is claimed, immediately before its upload
        begins; a job claimed after shutdown began is released unattempted.
        """
        lease = self.queue.claim_repository(
            repository, now=self.clock(), lease_seconds=REPOSITORY_LEASE_SECONDS
        )
        if lease is None:
            return 0
        held = HeldLeases(
            self.queue,
            repository,
            lease,
            clock=self.clock,
            lease_seconds=REPOSITORY_LEASE_SECONDS,
            renewal_seconds=self.lease_renewal_seconds,
        ).start()
        try:
            if stop_requested():
                return 0
            if api is None:
                self._credential_missing(
                    held,
                    job_kinds=job_kinds,
                    run_files=run_files,
                    at_shutdown=at_shutdown,
                )
                return 0
            hub = DatasetRepository(repository, api)
            job = self._claim_job(repository, job_kinds) if job_kinds else None
            if job is not None:
                if stop_requested():
                    # Shutdown began while the job was being claimed: give it
                    # back untouched instead of starting its upload.
                    self.queue.release_job(job)
                    return 0
                held.hold_job(job)
                return int(self._deliver_job(hub, held))
            if not self._run_files_to_commit(repository, run_files, at_shutdown):
                return 0
            return int(self._deliver_run_files(hub))
        finally:
            held.stop()
            self.queue.release_repository(repository, held.repository_lease)

    def _run_files_to_commit(
        self,
        repository: str,
        # Both calls in this class pass these two flags positionally; making them
        # keyword-only would change the method's call signature.
        run_files: bool,  # noqa: FBT001
        at_shutdown: bool,  # noqa: FBT001
    ) -> bool:
        return run_files and (
            at_shutdown or self.queue.run_files_due(repository, self.clock())
        )

    def _claim_job(self, repository: str, kinds: frozenset[str]) -> ClaimedJob | None:
        """Claim the next due job of ``kinds``, or return ``None``.

        Before the commit spacing has elapsed, only a release change is claimed.
        """
        now = self.clock()
        if not self.queue.commit_spacing_elapsed(repository, now):
            kinds = kinds & UNSPACED_JOB_KINDS
        if not kinds:
            return None
        return self.queue.claim_job(
            repository,
            now=now,
            lease_seconds=REPOSITORY_LEASE_SECONDS,
            kinds=kinds,
        )

    def _finish_job(
        self,
        job: ClaimedJob,
        *,
        status: str,
        error_code: str | None,
        retry: bool,
        committed: bool = False,
        revision: str | None = None,
        message: str | None = None,
        current: str | None = None,
    ) -> None:
        receipt: dict[str, Any] = {
            "handover_id": job.handover_id,
            "status": status,
            "error_code": error_code,
            "attempts": job.attempts + 1,
            "repository": job.target,
        }
        if job.kind == RELEASE_CHANGE:
            receipt.update(
                message=message,
                commit=revision,
                release=job.request["release"],
                build=job.request["build"],
                current=current,
            )
        else:
            receipt.update(
                revision=revision,
                prefix=f"{job.request['prefix']}/{job.request['run_id']}",
                copied_files=sorted(job.files),
            )
        self.queue.finish_job(
            job, receipt=receipt, retry=retry, now=self.clock(), committed=committed
        )

    def _deliver_job(self, hub: DatasetRepository, held: HeldLeases) -> bool:
        job = held.job
        if job is None:
            error_message = "The job is claimed before delivery."
            raise AssertionError(error_message)
        current = None
        if job.kind == RELEASE_CHANGE:
            outcome = self._release_outcome(hub, job)
            revision, message = outcome.commit, outcome.message
            current = outcome.current
        else:
            outcome = stage_bundle(hub, job.request, job.files)
            revision, message = outcome.revision, None
        if outcome.retry and outcome.error_code is not None:
            self.warn_once(
                outcome.error_code,
                UPLOAD_FAILED_MESSAGE.format(error_type=outcome.error_code),
            )
        held.stop()
        if held.job is None:
            raise AssertionError
        self._finish_job(
            held.job,
            status=outcome.status,
            error_code=outcome.error_code,
            retry=outcome.retry,
            committed=outcome.committed,
            revision=revision,
            message=message,
            current=current,
        )
        return outcome.committed

    def _release_outcome(
        self, hub: DatasetRepository, job: ClaimedJob
    ) -> ReleaseOutcome:
        """Apply a queued release change, unless it has waited too long."""
        expired = expired_change(job.created_at, self.clock())
        if expired is not None:
            return expired
        try:
            change = release_change_from_message(job.request)
        except RequestRefusedError as refusal:
            return ReleaseOutcome("refused", refusal.code)
        return apply_release_change(
            hub,
            change,
            published_at=iso_timestamp(self.clock()),
            requested_at=job.created_at,
            newest_local_requests=self._newest_current_changes_landed(job.target),
        )

    def _newest_current_changes_landed(self, repository: str) -> dict[str, float]:
        """Return when each release's newest landed ``current`` change was handed over.

        Only the release changes in this machine's queue for ``repository`` that
        are ``published`` and set ``current`` (see ``sets_current``) count.
        """
        newest_requests: dict[str, float] = {}
        for created_at, request in self.queue.requests_with_status(
            repository, kind=RELEASE_CHANGE, status="published"
        ):
            try:
                change = release_change_from_message(request)
            except RequestRefusedError:
                continue
            if sets_current(change):
                newest_requests[change.release] = max(
                    newest_requests.get(change.release, created_at), created_at
                )
        return newest_requests

    def _credential_missing(
        self,
        held: HeldLeases,
        *,
        job_kinds: frozenset[str],
        run_files: bool,
        at_shutdown: bool,
    ) -> None:
        repository = held.repository
        self.warn_once(CREDENTIAL_MISSING, CREDENTIAL_MISSING_MESSAGE)
        job = self._claim_job(repository, job_kinds) if job_kinds else None
        if job is not None:
            held.hold_job(job)
            held.stop()
            if held.job is None:
                raise AssertionError
            expired = (
                expired_change(job.created_at, self.clock())
                if job.kind == RELEASE_CHANGE
                else None
            )
            if expired is not None:
                # Final even without a credential: a later login must not
                # apply a change requested long before.
                self._finish_job(
                    held.job,
                    status=expired.status,
                    error_code=expired.error_code,
                    retry=False,
                    message=expired.message,
                )
                return
            self._finish_job(
                held.job, status="pending", error_code=CREDENTIAL_MISSING, retry=True
            )
            return
        if not self._run_files_to_commit(repository, run_files, at_shutdown):
            return
        entries = self.queue.pending_run_files(repository)
        index_entries = self.queue.pending_run_index_entries(repository)
        if entries or index_entries:
            self.queue.record_run_files_failed(
                repository,
                entries,
                index_entries,
                error_code=CREDENTIAL_MISSING,
                now=self.clock(),
            )

    def _deliver_run_files(self, hub: DatasetRepository) -> bool:
        entries = self.queue.pending_run_files(hub.repo_id)
        index_entries = self.queue.pending_run_index_entries(hub.repo_id)
        if not entries and not index_entries:
            return False
        try:
            revision = commit_run_files(
                hub,
                entries,
                index_entries,
                updated_at=iso_timestamp(self.clock()),
            )
        # Nothing here raises into the build: a failed commit keeps the run
        # files queued for a later cycle.
        except Exception as error:  # noqa: BLE001
            # Only the type name: Hub errors can carry URLs and identifiers.
            self.warn_once(
                UPLOAD_FAILED,
                UPLOAD_FAILED_MESSAGE.format(error_type=type(error).__name__),
            )
            self.queue.record_run_files_failed(
                hub.repo_id,
                entries,
                index_entries,
                error_code=UPLOAD_FAILED,
                now=self.clock(),
            )
            return False
        self.queue.record_run_files_committed(
            hub.repo_id, entries, index_entries, revision=revision, now=self.clock()
        )
        return True

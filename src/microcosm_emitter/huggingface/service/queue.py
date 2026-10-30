"""Durable hand-overs, kept in the Hugging Face module's hand-over database.

The hand-over database is its own SQLite file, next to the spool database
(``events.sqlite3`` → ``events.handovers.sqlite3``), with its own Alembic
history. The shared spool database stays at a revision that released
emitters accept: Microcosm builds pinned to one share it on the same machine.

Two record shapes share the hand-over database:

- **Staging run files** coalesce: each repository path keeps only the newest
  handed-over bytes, so a commit carries the latest state of every pending
  path at once. One commit per repository per cycle replaces Microcosm's one
  ``upload_file`` commit per file.
- **Jobs** (dataset bundles and release changes) follow the graph queue's
  pattern: a row per hand-over and destination, a lease while a worker holds
  it, retries with backoff, and a receipt.

A per-repository schedule spaces commits and leases the repository to one
writer on this machine at a time.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from microcosm_emitter.core.database import (
    create_spool_engine,
    create_spool_session_factory,
)
from microcosm_emitter.core.handover_models import (
    HandoverJob,
    RepositoryCommitSchedule,
    StagedRunDelivery,
    StagedRunFile,
)
from microcosm_emitter.core.migrations import upgrade_handover_database
from microcosm_emitter.huggingface.service.queue_jobs import (
    DESTINATION_HUGGING_FACE,
    JOB_PENDING,
    MAX_RETRY_SECONDS,
    ClaimedJob,
    HandoverJobs,
    repository_schedule,
    retry_delay,
    sha256_bytes,
)

# The job definitions moved to queue_jobs.py; importers keep reading them here.
__all__ = [
    "DESTINATION_HUGGING_FACE",
    "JOB_PENDING",
    "MAX_RETRY_SECONDS",
    "MIN_COMMIT_SPACING_SECONDS",
    "ClaimedJob",
    "HandoverQueue",
    "PendingRunFile",
    "now_seconds",
    "retry_delay",
    "sha256_bytes",
    "sha256_file",
]

#: The shortest time between two commits to one repository from this machine,
#: of run files or dataset bundles, even for hand-overs that ask to be
#: committed at the next cycle. A cycle checks it when it lists the
#: repository and again once it has leased it, because another thread may
#: have committed in between. Release changes, which are rare and which a
#: build waits for, ignore it, as does the commit at shutdown; other
#: machines' commits are not counted.
MIN_COMMIT_SPACING_SECONDS = 10.0


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of the file at ``path``, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_text(entry: Mapping[str, Any]) -> str:
    """Order-preserving JSON, so the merged index keeps the build's key order."""
    return json.dumps(dict(entry), separators=(",", ":"), allow_nan=False)


def _entry(text: str | None) -> dict[str, Any]:
    return {} if text is None else json.loads(text)


@dataclass(frozen=True)
class PendingRunFile:
    """The bytes a commit will write at one repository path."""

    path_in_repo: str
    run_id: str
    content: bytes
    sha256: str


class HandoverQueue(HandoverJobs):
    """Hand-over records in the hand-over database at ``path``.

    Rows are kept after delivery; nothing deletes them yet. Settled jobs keep
    their receipts, committed run files keep their path and committed digest
    (their bytes are cleared), and run deliveries and repository schedules
    keep their counts and times. Only the bytes copied for a hand-over
    (``handover_files``) are deleted, once every destination has settled.
    """

    def __init__(self, path: Path | str) -> None:
        """Open the hand-over database at ``path``, applying its migrations.

        The parent folder is created if it is missing.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_spool_engine(self.path)
        upgrade_handover_database(self._engine)
        self._sessions = create_spool_session_factory(self._engine)

    def close(self) -> None:
        """Release this handle's database connections without deleting records."""
        self._engine.dispose()

    # Repository schedules ------------------------------------------------

    def claim_repository(
        self, repository: str, *, now: float, lease_seconds: float
    ) -> float | None:
        """Lease the repository to this writer; ``None`` while another holds it."""
        with self._sessions.begin() as session:
            repository_schedule(session, repository)
            lease = now + lease_seconds
            claimed = session.scalar(
                update(RepositoryCommitSchedule)
                .where(
                    RepositoryCommitSchedule.repository == repository,
                    RepositoryCommitSchedule.lease_until <= now,
                )
                .values(lease_until=lease)
                .returning(RepositoryCommitSchedule.repository)
            )
            return lease if claimed is not None else None

    def renew_repository(self, repository: str, lease: float, renewed: float) -> bool:
        """Extend this writer's lease to ``renewed``; False if it was lost."""
        with self._sessions.begin() as session:
            return (
                session.scalar(
                    update(RepositoryCommitSchedule)
                    .where(
                        RepositoryCommitSchedule.repository == repository,
                        RepositoryCommitSchedule.lease_until == lease,
                    )
                    .values(lease_until=renewed)
                    .returning(RepositoryCommitSchedule.repository)
                )
                is not None
            )

    def repository_lease(self, repository: str) -> float:
        """When the repository's current lease expires; 0.0 when none is held."""
        with self._sessions() as session:
            schedule = session.get(RepositoryCommitSchedule, repository)
            return 0.0 if schedule is None else schedule.lease_until

    def release_repository(self, repository: str, lease: float) -> None:
        """Free the repository's lease if it still ends at ``lease``.

        A lease another writer has taken since is left as it is.
        """
        with self._sessions.begin() as session:
            session.execute(
                update(RepositoryCommitSchedule)
                .where(
                    RepositoryCommitSchedule.repository == repository,
                    RepositoryCommitSchedule.lease_until == lease,
                )
                .values(lease_until=0.0)
            )

    def record_commit(self, repository: str, *, now: float) -> None:
        """Record ``now`` as the time of the last commit to ``repository``.

        The repository's schedule is created if it has none.
        """
        with self._sessions.begin() as session:
            repository_schedule(session, repository).last_commit_at = now

    def due_repositories(
        self,
        now: float,
        *,
        kinds: Iterable[str] | None = None,
        unspaced_kinds: Iterable[str] = (),
        run_files: bool = True,
    ) -> list[str]:
        """Repositories with work due now that may receive a commit now.

        ``kinds`` limits jobs to those this emitter can deliver, so a job
        queued by a newer emitter sharing the database waits for it; an empty
        ``kinds`` leaves jobs out. ``run_files`` false leaves out run files.
        Every commit waits ``MIN_COMMIT_SPACING_SECONDS`` after this machine's
        last commit to the repository, except jobs of ``unspaced_kinds``.
        """
        job_kinds = None if kinds is None else list(kinds)
        unspaced = set(unspaced_kinds)
        with self._sessions() as session:
            run_file_repositories: set[str] = set()
            if run_files:
                run_file_repositories = set(
                    session.scalars(
                        select(StagedRunFile.repository).where(
                            StagedRunFile.pending.is_(True),
                            StagedRunFile.due_at <= now,
                        )
                    )
                ) | set(
                    session.scalars(
                        select(StagedRunDelivery.repository).where(
                            StagedRunDelivery.run_index_pending.is_(True),
                            StagedRunDelivery.run_index_due_at <= now,
                        )
                    )
                )
            spaced_job_repositories: set[str] = set()
            unspaced_job_repositories: set[str] = set()
            if job_kinds != []:
                job_query = select(HandoverJob.target, HandoverJob.kind).where(
                    HandoverJob.destination == DESTINATION_HUGGING_FACE,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.next_attempt_at <= now,
                    HandoverJob.lease_until <= now,
                )
                if job_kinds is not None:
                    job_query = job_query.where(HandoverJob.kind.in_(job_kinds))
                for target, kind in session.execute(job_query):
                    if kind in unspaced:
                        unspaced_job_repositories.add(target)
                    else:
                        spaced_job_repositories.add(target)
            due = []
            for repository in sorted(
                run_file_repositories
                | spaced_job_repositories
                | unspaced_job_repositories
            ):
                schedule = session.get(RepositoryCommitSchedule, repository)
                if schedule is not None and schedule.lease_until > now:
                    continue
                spaced = self._spacing_elapsed(schedule, now)
                if (
                    repository in unspaced_job_repositories
                    or (repository in spaced_job_repositories and spaced)
                    or (
                        repository in run_file_repositories
                        and self._run_files_allowed(schedule, now)
                    )
                ):
                    due.append(repository)
            return due

    @staticmethod
    def _spacing_elapsed(schedule: RepositoryCommitSchedule | None, now: float) -> bool:
        """Whether this machine's last commit to the repository is far enough back.

        The spacing keeps bursts of commits, each starting the dataset
        repository's webhook, from reaching the Hub; only release changes,
        which are rare and explicit, skip it.
        """
        return (
            schedule is None
            or schedule.last_commit_at + MIN_COMMIT_SPACING_SECONDS <= now
        )

    def commit_spacing_elapsed(self, repository: str, now: float) -> bool:
        """Return whether the commit spacing for ``repository`` has elapsed.

        True when the repository has no schedule, or when this machine's last
        commit to it was at least ``MIN_COMMIT_SPACING_SECONDS`` before ``now``.
        """
        with self._sessions() as session:
            return self._spacing_elapsed(
                session.get(RepositoryCommitSchedule, repository), now
            )

    @classmethod
    def _run_files_allowed(
        cls, schedule: RepositoryCommitSchedule | None, now: float
    ) -> bool:
        """Run files wait out failure backoff and the spacing between commits."""
        return cls._spacing_elapsed(schedule, now) and (
            schedule is None or schedule.retry_after <= now
        )

    # Staging run files ---------------------------------------------------

    @staticmethod
    def _delivery(
        session: Session, repository: str, run_id: str, now: float
    ) -> StagedRunDelivery:
        delivery = session.get(StagedRunDelivery, (repository, run_id))
        if delivery is None:
            delivery = StagedRunDelivery(
                repository=repository,
                run_id=run_id,
                files_attempted=0,
                files_committed=0,
                commit_attempts=0,
                commits=0,
                last_error_code=None,
                last_commit=None,
                run_index_entry=None,
                run_index_pending=False,
                run_index_due_at=0.0,
                updated_at=now,
            )
            session.add(delivery)
            session.flush()
        return delivery

    def stage_run_files(
        self,
        *,
        repository: str,
        run_id: str,
        files: Sequence[tuple[str, bytes]],
        run_index_entry: Mapping[str, Any] | None,
        flush: bool,
        interval: float,
        now: float,
    ) -> None:
        """Record the newest bytes per path; nothing reaches the Hub here.

        An unforced hand-over is due one ``interval`` after the repository's
        last commit; a forced one (``flush``) at the next cycle.
        """
        with self._sessions.begin() as session:
            schedule = repository_schedule(session, repository)
            due_at = now if flush else max(now, schedule.last_commit_at + interval)
            for path_in_repo, content in files:
                digest = sha256_bytes(content)
                row = session.get(StagedRunFile, (repository, path_in_repo))
                if row is None:
                    session.add(
                        StagedRunFile(
                            repository=repository,
                            path_in_repo=path_in_repo,
                            run_id=run_id,
                            content=content,
                            content_sha256=digest,
                            committed_sha256=None,
                            pending=True,
                            due_at=due_at,
                            updated_at=now,
                        )
                    )
                    continue
                if digest == row.committed_sha256:
                    # The repository already holds exactly these bytes. If
                    # other bytes for this path are pending, a commit of them
                    # may be in progress: keep these bytes, which
                    # record_run_files_committed queues again after it.
                    row.content = content if row.pending else b""
                    row.pending = False
                elif not (row.pending and digest == row.content_sha256):
                    row.due_at = min(row.due_at, due_at) if row.pending else due_at
                    row.content, row.pending = content, True
                row.content_sha256 = digest
                row.run_id, row.updated_at = run_id, now
            delivery = self._delivery(session, repository, run_id, now)
            if run_index_entry is not None:
                entry = _entry_text(run_index_entry)
                if delivery.run_index_entry != entry or delivery.run_index_pending:
                    delivery.run_index_due_at = (
                        min(delivery.run_index_due_at, due_at)
                        if delivery.run_index_pending
                        else due_at
                    )
                    delivery.run_index_entry = entry
                    delivery.run_index_pending = True
            if flush:
                session.execute(
                    update(StagedRunFile)
                    .where(
                        StagedRunFile.repository == repository,
                        StagedRunFile.run_id == run_id,
                        StagedRunFile.pending.is_(True),
                        StagedRunFile.due_at > now,
                    )
                    .values(due_at=now)
                )
                if delivery.run_index_pending:
                    delivery.run_index_due_at = min(delivery.run_index_due_at, now)
            delivery.updated_at = now

    def run_files_due(self, repository: str, now: float) -> bool:
        """Whether pending run files are due, past backoff and commit spacing."""
        with self._sessions() as session:
            schedule = session.get(RepositoryCommitSchedule, repository)
            if not self._run_files_allowed(schedule, now):
                return False
            due_file = session.scalar(
                select(StagedRunFile.path_in_repo)
                .where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.pending.is_(True),
                    StagedRunFile.due_at <= now,
                )
                .limit(1)
            )
            due_index = session.scalar(
                select(StagedRunDelivery.run_id)
                .where(
                    StagedRunDelivery.repository == repository,
                    StagedRunDelivery.run_index_pending.is_(True),
                    StagedRunDelivery.run_index_due_at <= now,
                )
                .limit(1)
            )
            return due_file is not None or due_index is not None

    def repositories_with_pending_run_files(self) -> list[str]:
        """List the repositories with a pending run file or run index entry, sorted.

        Due times are not checked.
        """
        with self._sessions() as session:
            return sorted(
                set(
                    session.scalars(
                        select(StagedRunFile.repository).where(
                            StagedRunFile.pending.is_(True)
                        )
                    )
                )
                | set(
                    session.scalars(
                        select(StagedRunDelivery.repository).where(
                            StagedRunDelivery.run_index_pending.is_(True)
                        )
                    )
                )
            )

    def pending_run_files(self, repository: str) -> list[PendingRunFile]:
        """Return the pending run files of ``repository``, ordered by path."""
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunFile)
                .where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.pending.is_(True),
                )
                .order_by(StagedRunFile.path_in_repo)
            )
            return [
                PendingRunFile(
                    row.path_in_repo, row.run_id, row.content, row.content_sha256
                )
                for row in rows
            ]

    def pending_run_index_entries(
        self, repository: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return the pending run index entries of ``repository``, sorted by run.

        Each item is a run ID and that run's entry as a dictionary.
        """
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunDelivery)
                .where(
                    StagedRunDelivery.repository == repository,
                    StagedRunDelivery.run_index_pending.is_(True),
                )
                .order_by(StagedRunDelivery.run_id)
            )
            return [(row.run_id, _entry(row.run_index_entry)) for row in rows]

    @staticmethod
    def _files_per_run(entries: Iterable[PendingRunFile]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.run_id] = counts.get(entry.run_id, 0) + 1
        return counts

    def record_run_files_committed(
        self,
        repository: str,
        entries: Sequence[PendingRunFile],
        index_entries: Sequence[tuple[str, Mapping[str, Any]]],
        *,
        revision: str,
        now: float,
    ) -> None:
        """Settle what the commit wrote; newer hand-overs stay pending."""
        with self._sessions.begin() as session:
            for entry in entries:
                row = session.get(StagedRunFile, (repository, entry.path_in_repo))
                if row is None:
                    continue
                row.committed_sha256 = entry.sha256
                if row.content_sha256 == entry.sha256:
                    row.pending, row.content = False, b""
                else:
                    # Other bytes were handed over during this commit.
                    row.pending = True
            counts = self._files_per_run(entries)
            index_runs = {run_id for run_id, _ in index_entries}
            for run_id in sorted(set(counts) | index_runs):
                delivery = self._delivery(session, repository, run_id, now)
                delivery.files_attempted += counts.get(run_id, 0)
                delivery.files_committed += counts.get(run_id, 0)
                delivery.commit_attempts += 1
                delivery.commits += 1
                delivery.last_commit = revision
                delivery.last_error_code = None
                delivery.updated_at = now
            for run_id, entry in index_entries:
                delivery = self._delivery(session, repository, run_id, now)
                if delivery.run_index_entry == _entry_text(entry):
                    delivery.run_index_pending = False
            schedule = repository_schedule(session, repository)
            schedule.last_commit_at = now
            schedule.consecutive_failures = 0
            schedule.retry_after = 0.0

    def record_run_files_failed(
        self,
        repository: str,
        entries: Sequence[PendingRunFile],
        index_entries: Sequence[tuple[str, Mapping[str, Any]]],
        *,
        error_code: str,
        now: float,
    ) -> None:
        """Keep everything pending and back off before the next attempt."""
        with self._sessions.begin() as session:
            counts = self._files_per_run(entries)
            index_runs = {run_id for run_id, _ in index_entries}
            for run_id in sorted(set(counts) | index_runs):
                delivery = self._delivery(session, repository, run_id, now)
                delivery.files_attempted += counts.get(run_id, 0)
                delivery.commit_attempts += 1
                delivery.last_error_code = error_code
                delivery.updated_at = now
            schedule = repository_schedule(session, repository)
            schedule.consecutive_failures += 1
            schedule.retry_after = now + retry_delay(schedule.consecutive_failures)

    def run_receipt(self, repository: str, run_id: str) -> dict[str, Any]:
        """Return the receipt of one run: pending files, attempt and commit counts.

        A pending run index entry counts as one pending file. A run with no
        delivery record gets zero counts and no last commit or error code.
        """
        with self._sessions() as session:
            delivery = session.get(StagedRunDelivery, (repository, run_id))
            pending = len(
                list(
                    session.scalars(
                        select(StagedRunFile.path_in_repo).where(
                            StagedRunFile.repository == repository,
                            StagedRunFile.run_id == run_id,
                            StagedRunFile.pending.is_(True),
                        )
                    )
                )
            )
            if delivery is not None and delivery.run_index_pending:
                pending += 1
            return {
                "repository": repository,
                "run_id": run_id,
                "pending_files": pending,
                "files_attempted": delivery.files_attempted if delivery else 0,
                "files_committed": delivery.files_committed if delivery else 0,
                "commit_attempts": delivery.commit_attempts if delivery else 0,
                "commits": delivery.commits if delivery else 0,
                "last_error_code": delivery.last_error_code if delivery else None,
                "last_commit": delivery.last_commit if delivery else None,
            }

    def committed_run_files(self, repository: str, run_id: str) -> dict[str, str]:
        """Each committed path of the run and the digest of its committed bytes."""
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunFile).where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.run_id == run_id,
                    StagedRunFile.committed_sha256.is_not(None),
                )
            )
            return {row.path_in_repo: str(row.committed_sha256) for row in rows}


def now_seconds() -> float:
    """Wall-clock seconds, shared by every persisted queue time."""
    return time.time()

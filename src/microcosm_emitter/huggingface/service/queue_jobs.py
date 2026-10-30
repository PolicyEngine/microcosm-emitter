"""Jobs in the Hugging Face module's hand-over database.

Dataset bundles and release changes follow the graph queue's pattern: a row
per hand-over and destination, a lease while a worker holds it, retries with
backoff, and a receipt. ``HandoverQueue`` in ``queue.py`` keeps these with the
staging run files and repository schedules, and inherits the methods below.

The definitions those parts share with the jobs live here too: the retry
backoff, the destination and pending status, content digests and the
repository schedule row. ``queue.py`` imports them, so every name it offered
before the jobs moved here is still importable from it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from microcosm_emitter.core.handover_models import (
    HandoverFile,
    HandoverJob,
    RepositoryCommitSchedule,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.orm import Session, sessionmaker

MAX_RETRY_SECONDS = 300.0
DESTINATION_HUGGING_FACE = "huggingface"
JOB_PENDING = "pending"


def sha256_bytes(content: bytes) -> str:
    """Return the hex SHA-256 digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


def retry_delay(failures: int) -> float:
    """2, 4, 8 … seconds after each failure, at most ``MAX_RETRY_SECONDS``."""
    return min(MAX_RETRY_SECONDS, float(2 ** min(failures, 16)))


def repository_schedule(session: Session, repository: str) -> RepositoryCommitSchedule:
    """Return the repository's commit schedule, added with zero times if it has none."""
    schedule = session.get(RepositoryCommitSchedule, repository)
    if schedule is None:
        schedule = RepositoryCommitSchedule(
            repository=repository,
            last_commit_at=0.0,
            retry_after=0.0,
            consecutive_failures=0,
            lease_until=0.0,
        )
        session.add(schedule)
        session.flush()
    return schedule


@dataclass(frozen=True)
class ClaimedJob:
    """A job leased until ``lease_until``; the value also identifies the lease."""

    handover_id: str
    destination: str
    kind: str
    target: str
    request: dict[str, Any]
    attempts: int
    lease_until: float
    #: The bytes of the files copied at hand-over, by name.
    files: dict[str, bytes] = field(default_factory=dict)
    #: When the job was handed over, by this machine's clock.
    created_at: float = 0.0


class HandoverJobs:
    """The job records of the hand-over database.

    ``HandoverQueue`` sets ``_sessions`` when it opens the database.
    """

    _sessions: sessionmaker[Session]

    def enqueue_job(
        self,
        *,
        handover_id: str,
        kind: str,
        target: str,
        request: Mapping[str, Any],
        receipt: Mapping[str, Any],
        now: float,
        destination: str = DESTINATION_HUGGING_FACE,
        files: Mapping[str, bytes] | None = None,
        equivalent_targets: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Durably record a hand-over once; repeating it returns its receipt.

        ``files`` are the bytes copied at hand-over, stored with the job in
        one transaction and kept until every destination has settled. A job
        already queued for one of ``equivalent_targets``, other names of the
        same target, is the same hand-over.
        """
        with self._sessions.begin() as session:
            existing = session.get(HandoverJob, (handover_id, destination))
            if existing is not None:
                if (
                    existing.request != dict(request)
                    or existing.kind != kind
                    or existing.target not in {target, *equivalent_targets}
                ):
                    error_message = (
                        "Hand-over identity conflicts with a queued hand-over."
                    )
                    raise ValueError(error_message)
                return dict(existing.receipt)
            session.add(
                HandoverJob(
                    handover_id=handover_id,
                    destination=destination,
                    kind=kind,
                    target=target,
                    request=dict(request),
                    status=JOB_PENDING,
                    attempts=0,
                    next_attempt_at=0.0,
                    lease_until=0.0,
                    receipt=dict(receipt),
                    created_at=now,
                )
            )
            for name, content in sorted((files or {}).items()):
                if session.get(HandoverFile, (handover_id, name)) is None:
                    session.add(
                        HandoverFile(
                            handover_id=handover_id,
                            name=name,
                            content=content,
                            content_sha256=sha256_bytes(content),
                        )
                    )
            return dict(receipt)

    def copied_files(self, handover_id: str) -> dict[str, bytes]:
        """Return the bytes copied when ``handover_id`` was handed over, by name."""
        with self._sessions() as session:
            rows = session.scalars(
                select(HandoverFile).where(HandoverFile.handover_id == handover_id)
            )
            return {row.name: row.content for row in rows}

    def claim_job(
        self,
        target: str,
        *,
        now: float,
        lease_seconds: float,
        destination: str = DESTINATION_HUGGING_FACE,
        kinds: Iterable[str] | None = None,
        equivalent_targets: Iterable[str] = (),
    ) -> ClaimedJob | None:
        """Lease and return the earliest-due pending job for ``target``, if any.

        A job queued for one of ``equivalent_targets``, other names of the same
        target, counts too, and ``kinds`` limits the job kinds. A conditional
        update allows one current lease; a worker that exits leaves its job
        eligible again once the lease expires. ``None`` when no job is due or
        another writer leased it first.
        """
        with self._sessions.begin() as session:
            statement = select(HandoverJob).where(
                HandoverJob.destination == destination,
                HandoverJob.target.in_({target, *equivalent_targets}),
                HandoverJob.status == JOB_PENDING,
                HandoverJob.next_attempt_at <= now,
                HandoverJob.lease_until <= now,
            )
            if kinds is not None:
                statement = statement.where(HandoverJob.kind.in_(list(kinds)))
            candidate = session.scalar(
                statement.order_by(
                    HandoverJob.next_attempt_at, HandoverJob.created_at
                ).limit(1)
            )
            if candidate is None:
                return None
            lease = now + lease_seconds
            claimed = session.scalar(
                update(HandoverJob)
                .where(
                    HandoverJob.handover_id == candidate.handover_id,
                    HandoverJob.destination == destination,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.lease_until <= now,
                )
                .values(lease_until=lease)
                .returning(HandoverJob.handover_id)
            )
            if claimed is None:
                return None
            session.refresh(candidate)
            copied = session.scalars(
                select(HandoverFile).where(
                    HandoverFile.handover_id == candidate.handover_id
                )
            )
            return ClaimedJob(
                handover_id=candidate.handover_id,
                destination=destination,
                kind=candidate.kind,
                target=candidate.target,
                request=dict(candidate.request),
                attempts=candidate.attempts,
                lease_until=lease,
                files={row.name: row.content for row in copied},
                created_at=candidate.created_at,
            )

    def renew_job(self, job: ClaimedJob, renewed: float) -> bool:
        """Extend ``job``'s lease to ``renewed``; False if it was lost."""
        with self._sessions.begin() as session:
            return (
                session.scalar(
                    update(HandoverJob)
                    .where(
                        HandoverJob.handover_id == job.handover_id,
                        HandoverJob.destination == job.destination,
                        HandoverJob.status == JOB_PENDING,
                        HandoverJob.lease_until == job.lease_until,
                    )
                    .values(lease_until=renewed)
                    .returning(HandoverJob.handover_id)
                )
                is not None
            )

    def release_job(self, job: ClaimedJob) -> None:
        """Give back ``job``'s lease without recording an attempt.

        The job stays pending and due, so any emitter may claim it at once.
        """
        with self._sessions.begin() as session:
            session.execute(
                update(HandoverJob)
                .where(
                    HandoverJob.handover_id == job.handover_id,
                    HandoverJob.destination == job.destination,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.lease_until == job.lease_until,
                )
                .values(lease_until=0.0)
            )

    def finish_job(
        self,
        job: ClaimedJob,
        *,
        receipt: Mapping[str, Any],
        retry: bool,
        now: float,
        committed: bool = False,
        retry_after: float | None = None,
    ) -> bool:
        """Record one attempt; only the current lease holder may do so.

        A retried job waits ``retry_after`` seconds when given, otherwise the
        backoff for its number of attempts. Returns whether it was recorded:
        False when the lease was lost.
        """
        with self._sessions.begin() as session:
            row = session.scalar(
                update(HandoverJob)
                .where(
                    HandoverJob.handover_id == job.handover_id,
                    HandoverJob.destination == job.destination,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.lease_until == job.lease_until,
                )
                .values(attempts=HandoverJob.attempts + 1, lease_until=0.0)
                .returning(HandoverJob)
            )
            if row is None:
                return False
            row.receipt = dict(receipt)
            if retry:
                row.next_attempt_at = now + (
                    retry_delay(row.attempts) if retry_after is None else retry_after
                )
            else:
                row.status = str(receipt["status"])
                still_pending = session.scalar(
                    select(HandoverJob.destination)
                    .where(
                        HandoverJob.handover_id == job.handover_id,
                        HandoverJob.status == JOB_PENDING,
                    )
                    .limit(1)
                )
                if still_pending is None:
                    # Every destination has settled: the copies are not needed.
                    session.execute(
                        delete(HandoverFile).where(
                            HandoverFile.handover_id == job.handover_id
                        )
                    )
            if committed:
                repository_schedule(session, job.target).last_commit_at = now
            return True

    def requests_with_status(
        self,
        target: str,
        *,
        kind: str,
        status: str,
        destination: str = DESTINATION_HUGGING_FACE,
    ) -> list[tuple[float, dict[str, Any]]]:
        """List the jobs of ``kind`` for ``target`` whose status is now ``status``.

        Each item is the time the job was handed over, by this machine's clock,
        and its request.
        """
        with self._sessions() as session:
            rows = session.execute(
                select(HandoverJob.created_at, HandoverJob.request).where(
                    HandoverJob.destination == destination,
                    HandoverJob.target == target,
                    HandoverJob.kind == kind,
                    HandoverJob.status == status,
                )
            )
            return [(created_at, dict(request)) for created_at, request in rows]

    def job_receipt(
        self, handover_id: str, destination: str = DESTINATION_HUGGING_FACE
    ) -> dict[str, Any] | None:
        """Return the job receipt for ``handover_id``, or ``None`` if none is queued."""
        with self._sessions() as session:
            job = session.get(HandoverJob, (handover_id, destination))
            return None if job is None else dict(job.receipt)

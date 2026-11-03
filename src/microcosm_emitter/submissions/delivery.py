"""Deliver bundle hand-overs to the collector, after their Hugging Face commit.

A bundle hand-over that describes its build gets a second row in the
hand-over queue, for the ``collector`` destination. It waits until the same
hand-over's Hugging Face row has staged the bundle, because the submission's
provenance names that commit. Then each planned submission is submitted until
the collector accepts or refuses it.

Accepted submissions stay in the queue, ``awaiting_final_status``, and their
status is checked with the same row's leases, attempts and retries: the
first check ``FIRST_STATUS_CHECK_SECONDS`` after acceptance, each later wait
twice the last, up to ``MAX_STATUS_CHECK_SECONDS``. Once the collector reports
a final status, its receipt is stored in the row's receipt, with the
collector's reason for a ``rejected`` or ``failed`` submission. A submission
still not final ``FINAL_STATUS_TIME_LIMIT_SECONDS`` after acceptance is
``timed_out``. Each submission that ends ``rejected``, ``failed`` or
``timed_out`` is reported on standard error. The row lives in the hand-over
database, so an emitter that exits first leaves the checks to the next
emitter started with that database whose origin reaches the same collector
(``same_collector_origins``).

Collector submissions are off unless the module configuration switches them
on, so an emitter release with this code changes nothing by default.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from microcosm_emitter.core.collector_origin import same_collector_origins
from microcosm_emitter.huggingface.service.delivery import HeldLeases, never_stop
from microcosm_emitter.submissions.client import (
    AWAITING_FINAL_STATUS,
    FINAL_STATUSES,
    OWNED_BY_ANOTHER_USER,
    CollectorSubmissionClient,
)
from microcosm_emitter.submissions.planning import (
    MAX_FOLDER_CHARACTERS,
    PlannedSubmission,
    bundle_folder,
    planned_submissions,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from microcosm_emitter.huggingface.service.queue import ClaimedJob, HandoverQueue

# Each print in this module that suppresses Ruff's T201 writes one of its
# messages to standard error on purpose; none is leftover debugging output.

COLLECTOR_SUBMISSIONS_ENABLED_BY_DEFAULT: Final = False
DESTINATION_COLLECTOR: Final = "collector"
COLLECTOR_SUBMISSION: Final = "collector_submission"
#: How long a collector row's lease lasts without renewal. While an attempt
#: runs, its lease is renewed every ``SUBMISSION_LEASE_RENEWAL_SECONDS``, as
#: ``HeldLeases`` does for Hugging Face jobs, so another emitter on this
#: machine (they share the hand-over database) never claims the row while an
#: upload runs, however long it takes. Renewal stops when the attempt ends
#: or shutdown begins, so a row whose emitter was killed or stopped is free
#: again within one lease.
SUBMISSION_LEASE_SECONDS: Final = 120.0
SUBMISSION_LEASE_RENEWAL_SECONDS: Final = 30.0
MAX_JOBS_PER_CYCLE: Final = 8
#: How long one cycle keeps starting submissions. The budget never stops a
#: submission in progress: an upload at or above the minimum rate described
#: at ``client.UPLOAD_TIMEOUT`` runs to its end, and only a request that gets
#: no answer, or an upload slower than that, fails.
MAX_CYCLE_SECONDS: Final = 600.0
STAGED_STATUSES: Final = frozenset({"uploaded", "already_staged"})
NOT_STAGED_STATUSES: Final = frozenset({"failed", "refused"})
WAITING_FOR_HUGGING_FACE: Final = "WAITING_FOR_HUGGING_FACE"
NOT_STAGED_ON_HUGGING_FACE: Final = "NOT_STAGED_ON_HUGGING_FACE"
COLLECTOR_FOLDER_TOO_LONG: Final = "COLLECTOR_FOLDER_TOO_LONG"
#: The emitter hit an unexpected error of its own during an attempt.
LOCAL_ERROR: Final = "LOCAL_ERROR"
#: When an accepted submission's status is first checked, and the longest
#: wait between two checks; each wait is twice the last.
FIRST_STATUS_CHECK_SECONDS: Final = 10.0
MAX_STATUS_CHECK_SECONDS: Final = 600.0
#: How long after acceptance the emitter keeps checking a submission's
#: status. The collector's recovery brings every job to a final status within
#: three attempts or dispatches of about an hour each, so a day leaves a wide
#: margin, including for an emitter that is not running for part of it.
FINAL_STATUS_TIME_LIMIT_SECONDS: Final = 24 * 3600.0
#: A submission still not final at the time limit.
TIMED_OUT: Final = "timed_out"
FINAL_STATUS_TIMED_OUT: Final = "FINAL_STATUS_TIMED_OUT"
#: A submission in one of these states is never sent or checked again.
SETTLED_SUBMISSION_STATUSES: Final = FINAL_STATUSES | {TIMED_OUT}
#: Submission states reported on standard error when the hand-over settles.
REPORTED_SUBMISSION_STATUSES: Final = frozenset({"rejected", "failed", TIMED_OUT})
#: One line per such submission. The emitter host shares the build's
#: standard error, so it reaches the build's log.
SUBMISSION_OUTCOME_MESSAGE: Final = (
    "Microcosm collector submission {status}: {kind} for hand-over "
    "{handover_id} (submission {submission_id}), {error_code}. The hand-over's "
    "collector_submission_receipt has the details."
)


def print_to_standard_error(message: str) -> None:
    """Print ``message`` to standard error and flush it."""
    print(message, file=sys.stderr, flush=True)  # noqa: T201


def status_check_delay(checks_made: int) -> float:
    """How long to wait for the next status check after ``checks_made``."""
    return min(
        MAX_STATUS_CHECK_SECONDS,
        FIRST_STATUS_CHECK_SECONDS * float(2 ** min(checks_made, 16)),
    )


@dataclass(frozen=True)
class AttemptResult:
    """What one attempt at a collector row ends with."""

    status: str
    error_code: str | None
    submissions: list[dict[str, Any]]
    retry: bool
    retry_after: float | None = None


class CollectorSubmissionDelivery:
    """Queue and deliver the ``collector`` rows of bundle hand-overs."""

    def __init__(
        self,
        queue: HandoverQueue,
        client: CollectorSubmissionClient,
        *,
        clock: Callable[[], float] = time.time,
        report: Callable[[str], None] = print_to_standard_error,
    ) -> None:
        """Hold the hand-over queue, the collector client and the clock.

        ``report`` receives the message about each submission that ends
        without success; by default it prints to standard error.
        """
        self.queue, self.client, self.clock = queue, client, clock
        self.report = report

    @property
    def target(self) -> str:
        """The collector origin this delivery queues and claims rows under."""
        return self.client.origin

    @property
    def equivalent_targets(self) -> frozenset[str]:
        """The other origins of the same collector.

        Rows queued under one of them, such as rows that earlier releases
        queued under the Cloud Run address, are delivered through ``target``.
        """
        return same_collector_origins(self.target) - {self.target}

    def enqueue_for_bundle(
        self,
        handover_id: str,
        request: Mapping[str, Any],
        files: Mapping[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Queue the collector row; ``files`` are the bytes copied at hand-over.

        The copies are shared with the Hugging Face row and kept until both
        rows have settled.
        """
        return self.queue.enqueue_job(
            handover_id=handover_id,
            kind=COLLECTOR_SUBMISSION,
            target=self.target,
            request=request,
            receipt=self._receipt(handover_id, "pending", None, 0, []),
            now=self.clock(),
            destination=DESTINATION_COLLECTOR,
            files=files,
            equivalent_targets=self.equivalent_targets,
        )

    def receipt(self, handover_id: str) -> dict[str, Any] | None:
        """Return the collector row's receipt for ``handover_id``, or ``None``."""
        return self.queue.job_receipt(handover_id, DESTINATION_COLLECTOR)

    @staticmethod
    def _receipt(
        handover_id: str,
        status: str,
        error_code: str | None,
        attempts: int,
        submissions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "handover_id": handover_id,
            "destination": DESTINATION_COLLECTOR,
            "status": status,
            "error_code": error_code,
            "attempts": attempts,
            "submissions": submissions,
        }

    def run_cycle(self, *, stop_requested: Callable[[], bool] = never_stop) -> int:
        """Attempt each due collector hand-over once; return how many.

        The module runs this on a worker thread of its own, so a slow
        submission never delays a Hugging Face commit. Once
        ``stop_requested`` turns true (shutdown has begun), the cycle claims
        no further hand-over.
        """
        cycle_deadline = time.monotonic() + MAX_CYCLE_SECONDS
        attempted = 0
        for _ in range(MAX_JOBS_PER_CYCLE):
            if stop_requested():
                break
            if attempted and time.monotonic() >= cycle_deadline:
                break
            job = self.queue.claim_job(
                self.target,
                now=self.clock(),
                lease_seconds=SUBMISSION_LEASE_SECONDS,
                destination=DESTINATION_COLLECTOR,
                kinds=[COLLECTOR_SUBMISSION],
                equivalent_targets=self.equivalent_targets,
            )
            if job is None:
                break
            held = HeldLeases.for_job(
                self.queue,
                job,
                clock=self.clock,
                lease_seconds=SUBMISSION_LEASE_SECONDS,
                renewal_seconds=SUBMISSION_LEASE_RENEWAL_SECONDS,
                stop_requested=stop_requested,
            ).start()
            try:
                result = self._attempt(job)
            # Any failure is recorded as an attempt, so the row is retried after
            # the usual backoff instead of staying leased for the whole lease.
            except Exception:  # noqa: BLE001
                earlier = (self.receipt(job.handover_id) or {}).get("submissions")
                result = AttemptResult(
                    "pending", LOCAL_ERROR, list(earlier or []), retry=True
                )
            finally:
                held.stop()
            if held.job is None:
                error_message = "The held lease names the claimed job."
                raise AssertionError(error_message)
            # The renewed lease: only the holder of the current lease records.
            self._finish(held.job, result)
            attempted += 1
        return attempted

    def _finish(self, job: ClaimedJob, result: AttemptResult) -> None:
        receipt = self._receipt(
            job.handover_id,
            result.status,
            result.error_code,
            job.attempts + 1,
            result.submissions,
        )
        recorded = self.queue.finish_job(
            job,
            receipt=receipt,
            retry=result.retry,
            now=self.clock(),
            retry_after=result.retry_after,
        )
        if recorded and not result.retry:
            # The hand-over settled in this attempt: report each submission
            # that ended without success, once.
            for entry in result.submissions:
                if entry["status"] in REPORTED_SUBMISSION_STATUSES:
                    self.report(
                        SUBMISSION_OUTCOME_MESSAGE.format(
                            status=entry["status"],
                            kind=entry["kind"],
                            handover_id=job.handover_id,
                            submission_id=entry.get("submission_id") or "not created",
                            error_code=entry.get("error_code"),
                        )
                    )

    def _attempt(self, job: ClaimedJob) -> AttemptResult:
        if len(bundle_folder(job.request)) > MAX_FOLDER_CHARACTERS:
            # The collector refuses such a folder with a final 422.
            return AttemptResult("skipped", COLLECTOR_FOLDER_TOO_LONG, [], retry=False)
        previous = self.receipt(job.handover_id) or {}
        earlier = {entry["kind"]: entry for entry in previous.get("submissions", [])}
        staged = self.queue.job_receipt(job.handover_id)
        if staged is None or staged.get("status") in NOT_STAGED_STATUSES:
            return AttemptResult("skipped", NOT_STAGED_ON_HUGGING_FACE, [], retry=False)
        revision = staged.get("revision")
        if staged.get("status") not in STAGED_STATUSES or not revision:
            return AttemptResult(
                "pending",
                WAITING_FOR_HUGGING_FACE,
                list(earlier.values()),
                retry=True,
            )
        results: dict[str, dict[str, Any]] = {}
        waits: list[float] = []
        for planned in planned_submissions(job.request, revision, job.files):
            entry = earlier.get(planned.kind)
            if entry is not None and entry["status"] in SETTLED_SUBMISSION_STATUSES:
                results[planned.kind] = entry
                continue
            if entry is None or entry["status"] != AWAITING_FINAL_STATUS:
                entry = self._submit(planned, waits)
                if entry["collector_status"] in FINAL_STATUSES:
                    # Completion already answered with a final status: read
                    # the receipt now.
                    entry = self._check(entry, waits)
            else:
                entry = self._check(entry, waits)
            results[planned.kind] = entry
        return self._result(list(results.values()), waits)

    def _submit(self, planned: PlannedSubmission, waits: list[float]) -> dict[str, Any]:
        """Submit; an accepted submission then awaits its final status."""
        outcome = self.client.submit(planned)
        if outcome.retry_after is not None:
            waits.append(outcome.retry_after)
        accepted = outcome.status == "accepted"
        return {
            "kind": planned.kind,
            "status": AWAITING_FINAL_STATUS if accepted else outcome.status,
            "error_code": outcome.error_code,
            "submission_id": outcome.submission_id,
            "collector_status": outcome.collector_status,
            "rejection": None,
            "receipt": None,
            "status_checks": 0,
            "awaiting_final_status_since": self.clock() if accepted else None,
        }

    def _check(self, entry: Mapping[str, Any], waits: list[float]) -> dict[str, Any]:
        """Check an accepted submission's status; keep its receipt once final."""
        outcome = self.client.check_status(entry["submission_id"])
        if outcome.retry_after is not None:
            waits.append(outcome.retry_after)
        if outcome.status == "pending":
            # The collector lost it: the next attempt creates it again.
            return {
                **entry,
                "status": "pending",
                "error_code": outcome.error_code,
                "collector_status": None,
                "status_checks": 0,
                "awaiting_final_status_since": None,
            }
        checked = {
            **entry,
            "status": outcome.status,
            "error_code": outcome.error_code,
            "collector_status": outcome.collector_status
            or entry.get("collector_status"),
            "rejection": outcome.rejection,
            "receipt": outcome.receipt,
            "status_checks": int(entry.get("status_checks") or 0) + 1,
        }
        if checked["status"] == AWAITING_FINAL_STATUS and (
            self._seconds_left(checked) <= 0
        ):
            # Checked once more at or after the limit, so a final status the
            # collector already holds is still stored. A last check refused
            # because the token's Hugging Face user did not submit it keeps
            # that code: the collector has the submission and may have
            # finished it, which FINAL_STATUS_TIMED_OUT alone would hide.
            checked.update(
                status=TIMED_OUT,
                error_code=OWNED_BY_ANOTHER_USER
                if outcome.error_code == OWNED_BY_ANOTHER_USER
                else FINAL_STATUS_TIMED_OUT,
            )
        return checked

    def _seconds_left(self, entry: Mapping[str, Any]) -> float:
        """How long the time limit still allows checking ``entry``."""
        since = entry.get("awaiting_final_status_since")
        if not isinstance(since, int | float):
            since = self.clock()
        return since + FINAL_STATUS_TIME_LIMIT_SECONDS - self.clock()

    def _result(
        self, submissions: list[dict[str, Any]], waits: list[float]
    ) -> AttemptResult:
        """Derive the hand-over's state from its submissions' states.

        Still sending one of them is ``pending``; then waiting for one's final
        status is ``awaiting_final_status``. Once every one is settled, the
        hand-over is ``rejected`` if the collector rejected one, else
        ``failed`` if one failed, else ``timed_out`` if one timed out, else
        ``succeeded``; ``error_code`` is that submission's.
        """
        by_status: dict[str, list[dict[str, Any]]] = {}
        for entry in submissions:
            by_status.setdefault(entry["status"], []).append(entry)
        if "pending" in by_status:
            return AttemptResult(
                "pending",
                by_status["pending"][0]["error_code"],
                submissions,
                retry=True,
                retry_after=max(waits) if waits else None,
            )
        if AWAITING_FINAL_STATUS in by_status:
            awaiting = by_status[AWAITING_FINAL_STATUS]
            next_check = (
                max(waits)
                if waits
                else min(
                    status_check_delay(int(entry.get("status_checks") or 0))
                    for entry in awaiting
                )
            )
            # The last check falls on the time limit itself.
            seconds_left = min(self._seconds_left(entry) for entry in awaiting)
            return AttemptResult(
                AWAITING_FINAL_STATUS,
                awaiting[0]["error_code"],
                submissions,
                retry=True,
                retry_after=max(0.0, min(next_check, seconds_left)),
            )
        for final_status in ("rejected", "failed", TIMED_OUT):
            if final_status in by_status:
                return AttemptResult(
                    final_status,
                    by_status[final_status][0]["error_code"],
                    submissions,
                    retry=False,
                )
        return AttemptResult("succeeded", None, submissions, retry=False)

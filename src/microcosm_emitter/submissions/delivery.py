"""Deliver bundle hand-overs to the collector, after their Hugging Face commit.

A bundle hand-over that describes its build gets a second row in the
hand-over queue, for the ``collector`` destination. It waits until the same
hand-over's Hugging Face row has staged the bundle, because the submission's
provenance names that commit. Then each planned submission is submitted until
the collector accepts or refuses it.

Collector submissions are off unless the module configuration switches them
on, so an emitter release with this code changes nothing by default.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from microcosm_emitter.huggingface.service.delivery import HeldLeases, never_stop
from microcosm_emitter.huggingface.service.queue import ClaimedJob, HandoverQueue
from microcosm_emitter.submissions.client import CollectorSubmissionClient
from microcosm_emitter.submissions.planning import (
    MAX_FOLDER_CHARACTERS,
    bundle_folder,
    planned_submissions,
)

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
    ) -> None:
        self.queue, self.client, self.clock = queue, client, clock

    @property
    def target(self) -> str:
        return self.client.origin

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
        )

    def receipt(self, handover_id: str) -> dict[str, Any] | None:
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
            except Exception:
                # Record the attempt, so the row is retried after the usual
                # backoff instead of staying leased for the whole lease.
                earlier = (self.receipt(job.handover_id) or {}).get("submissions")
                result = AttemptResult(
                    "pending", LOCAL_ERROR, list(earlier or []), retry=True
                )
            finally:
                held.stop()
            assert held.job is not None, "The held lease names the claimed job."
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
        self.queue.finish_job(
            job,
            receipt=receipt,
            retry=result.retry,
            now=self.clock(),
            retry_after=result.retry_after,
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
            settled = earlier.get(planned.kind)
            if settled is not None and settled["status"] in {"accepted", "failed"}:
                results[planned.kind] = settled
                continue
            outcome = self.client.submit(planned)
            if outcome.retry_after is not None:
                waits.append(outcome.retry_after)
            results[planned.kind] = {
                "kind": planned.kind,
                "status": outcome.status,
                "error_code": outcome.error_code,
                "submission_id": outcome.submission_id,
                "collector_status": outcome.collector_status,
            }
        submissions = list(results.values())
        pending = [entry for entry in submissions if entry["status"] == "pending"]
        failed = [entry for entry in submissions if entry["status"] == "failed"]
        if pending:
            return AttemptResult(
                "pending",
                pending[0]["error_code"],
                submissions,
                retry=True,
                retry_after=max(waits) if waits else None,
            )
        if failed:
            return AttemptResult(
                "failed", failed[0]["error_code"], submissions, retry=False
            )
        return AttemptResult("submitted", None, submissions, retry=False)

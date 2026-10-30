"""The Hugging Face hand-over module of the local emitter host.

Requests are queries: the module checks and durably records a hand-over, then
answers with its receipt. Commits happen on worker threads:

- staging run-file commits, and the commit at shutdown, on the host's worker
  thread (``tick`` and ``close``);
- dataset bundle uploads and release changes, which can take minutes, each
  on the module's own ``DeliveryWorker``, so they never delay those (see
  ``workers``).
"""

from __future__ import annotations

import getpass
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from microcosm_emitter.core.auth import CollectorSession, huggingface_token
from microcosm_emitter.core.collector_origin import (
    DEVELOPMENT_COLLECTOR_URL_SETTING,
    configured_collector_origin,
)
from microcosm_emitter.host.constants import TICK_SECONDS
from microcosm_emitter.host.contracts import JsonObject, ModuleContext
from microcosm_emitter.huggingface.service.delivery import (
    CREDENTIAL_MISSING,
    DATASET_BUNDLE,
    HuggingFaceDelivery,
)
from microcosm_emitter.huggingface.service.hub import (
    DatasetRepository,
    hugging_face_api,
)
from microcosm_emitter.huggingface.service.queue import HandoverQueue, sha256_bytes
from microcosm_emitter.huggingface.service.releases import (
    ReleaseCaller,
    ReleaseChangePolicy,
    allow_every_change,
)
from microcosm_emitter.huggingface.service.requests import (
    RequestRefusedError,
    parse_release_change,
    parse_stage_dataset_bundle,
    parse_stage_run_files,
    require_supported_version,
    run_reference,
)
from microcosm_emitter.huggingface.service.workers import DeliveryWorker
from microcosm_emitter.submissions.client import (
    SUBMIT_SCOPE,
    CollectorSubmissionClient,
)
from microcosm_emitter.submissions.delivery import (
    COLLECTOR_SUBMISSION,
    COLLECTOR_SUBMISSIONS_ENABLED_BY_DEFAULT,
    CollectorSubmissionDelivery,
)
from microcosm_emitter.submissions.planning import wants_collector_submission
from microcosm_emitter_interface.identifiers import repository_identifier
from microcosm_emitter_interface.publication import RELEASE_CHANGE

#: A read-back of a run with no file this emitter committed: nothing to compare.
NO_COMMITTED_FILES = "NO_COMMITTED_FILES"
#: At shutdown, how long the module waits for a collector submission in
#: progress, after both Hugging Face shutdown commits and never past the
#: shutdown deadline. A submission still running then is abandoned; its row
#: is retried later.
COLLECTOR_SHUTDOWN_WAIT_SECONDS = 2.0
WORKER_ERROR_MESSAGE = (
    "Microcosm Hugging Face delivery hit a local queue error; handed-over files "
    "stay queued for a later attempt."
)
COLLECTOR_WORKER_ERROR_MESSAGE = (
    "Microcosm collector submission hit a local queue error; handed-over files "
    "stay queued for a later attempt."
)
COLLECTOR_QUEUE_ERROR_MESSAGE = (
    "A bundle could not be queued for the collector ({error_type}); its Hugging "
    "Face upload is queued as usual."
)
COLLECTOR_SETTING_MESSAGE = (
    "Microcosm collector submissions are off: the collector settings are "
    "unusable ({error_type}). Hugging Face uploads continue."
)


def _refused_run_files_receipt(message: Mapping[str, Any], code: str) -> dict[str, Any]:
    repository = message.get("repository")
    run_id = message.get("run_id")
    return {
        "status": "refused",
        "repository": repository if isinstance(repository, str) else "",
        "run_id": run_id if isinstance(run_id, str) else "",
        "pending_files": 0,
        "files_attempted": 0,
        "files_committed": 0,
        "commit_attempts": 0,
        "commits": 0,
        "last_error_code": None,
        "last_commit": None,
        "refusal_code": code,
    }


def _user_name() -> str:
    """Return the name of the account the emitter runs as, or its numeric user ID.

    ``getpass.getuser()`` raises when no user name variable is set and the
    user ID has no password-database entry, as in a container run with an
    arbitrary user ID (Python 3.13 and later).
    """
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid())


class HuggingFaceModule:
    """Queue hand-overs on request; commit them on worker threads.

    ``background_workers`` false runs every delivery on the host's ``tick``
    instead of starting the module's own workers; tests that drive the
    module by hand use it.

    With ``collector_submissions`` true in the configuration, bundles that
    describe their build are also submitted to the collector after their
    Hugging Face commit, at the origin ``configured_collector_origin`` chooses
    for every module: ``collector_origin`` selects one of the trusted
    collector origins, and ``development_collector_url`` points at a loopback
    collector. Collector submissions are off by default.
    """

    def __init__(
        self,
        configuration: JsonObject,
        context: ModuleContext,
        *,
        api_factory: Callable[[], Any | None] = hugging_face_api,
        clock: Callable[[], float] = time.time,
        release_change_policy: ReleaseChangePolicy = allow_every_change,
        background_workers: bool = True,
        worker_interval_seconds: float = TICK_SECONDS,
        collector_credential: Callable[[], str | None] = huggingface_token,
    ) -> None:
        """Hold the configuration and dependencies; ``initialize`` opens the queue."""
        self.configuration, self.context = configuration, context
        self.api_factory, self.clock = api_factory, clock
        self.release_change_policy = release_change_policy
        self.background_workers = background_workers
        self.worker_interval_seconds = worker_interval_seconds
        self.collector_credential = collector_credential
        self.queue: HandoverQueue | None = None
        self.delivery: HuggingFaceDelivery | None = None
        self.collector_delivery: CollectorSubmissionDelivery | None = None
        self.workers: dict[str, DeliveryWorker] = {}
        self._closed = False

    def initialize(self) -> None:
        """Open the hand-over queue, set up delivery and start any background workers.

        Collector submissions are set up only when ``collector_submissions`` is
        true. An unusable collector setting prints a warning and leaves them off.
        """
        # The hand-over database, never the shared spool database (see
        # ``HandoverQueue``); ``HuggingFaceSettings`` names it.
        self.queue = HandoverQueue(Path(self.configuration["handover_database_path"]))
        self.delivery = HuggingFaceDelivery(
            self.queue,
            api_factory=self.api_factory,
            clock=self.clock,
        )
        enabled = self.configuration.get(
            "collector_submissions", COLLECTOR_SUBMISSIONS_ENABLED_BY_DEFAULT
        )
        if enabled is True:
            try:
                self.collector_delivery = self._collector_delivery(self.queue)
            except ValueError as error:
                # A collector setting never stops the Hugging Face module.
                self.delivery.warn_once(
                    "collector settings",
                    COLLECTOR_SETTING_MESSAGE.format(error_type=type(error).__name__),
                )
        if self.background_workers:
            self.workers = self._delivery_workers(self.delivery)
            for worker in self.workers.values():
                worker.start()

    def _delivery_workers(
        self, delivery: HuggingFaceDelivery
    ) -> dict[str, DeliveryWorker]:
        """One worker per kind of upload that can take minutes.

        Collector submissions get their own: a slow or stalled submission
        never delays a Hugging Face commit, the commit at shutdown or a
        read-back.
        """
        workers = {
            DATASET_BUNDLE: DeliveryWorker(
                "dataset-bundles",
                lambda stop_requested: delivery.job_cycle(
                    frozenset({DATASET_BUNDLE}), stop_requested=stop_requested
                ),
                interval_seconds=self.worker_interval_seconds,
                on_error=self._worker_error,
            ),
            # A publication uploads release files too; it never waits for a
            # bundle upload to another repository, nor holds one up.
            RELEASE_CHANGE: DeliveryWorker(
                "release-changes",
                lambda stop_requested: delivery.job_cycle(
                    frozenset({RELEASE_CHANGE}), stop_requested=stop_requested
                ),
                interval_seconds=self.worker_interval_seconds,
                on_error=self._worker_error,
            ),
        }
        if self.collector_delivery is not None:
            workers[COLLECTOR_SUBMISSION] = DeliveryWorker(
                "collector-submissions",
                lambda stop_requested: self.collector_delivery.run_cycle(
                    stop_requested=stop_requested
                ),
                interval_seconds=self.worker_interval_seconds,
                on_error=self._collector_error,
            )
        return workers

    def _worker_error(self) -> None:
        if self.delivery is not None:
            self.delivery.warn_once("worker", WORKER_ERROR_MESSAGE)

    def _collector_error(self) -> None:
        if self.delivery is not None:
            self.delivery.warn_once("collector", COLLECTOR_WORKER_ERROR_MESSAGE)

    def _collector_delivery(self, queue: HandoverQueue) -> CollectorSubmissionDelivery:
        # The same origin the telemetry and Orrery modules resolve; an
        # untrusted origin raises ValueError, which turns submissions off.
        origin = configured_collector_origin(self.configuration)
        development_url = self.configuration.get(DEVELOPMENT_COLLECTOR_URL_SETTING)
        session = CollectorSession(
            origin, credential=self.collector_credential, scope=SUBMIT_SCOPE
        )
        client = CollectorSubmissionClient(
            origin,
            credential=session.credential,
            invalidate_credential=session.invalidate,
            allow_loopback_uploads=bool(development_url),
            clock=self.clock,
        )
        return CollectorSubmissionDelivery(queue, client, clock=self.clock)

    def handle_message(self, message: JsonObject) -> None:
        """Raise ``ValueError``: Hugging Face hand-overs are queries with receipts."""
        error_message = "Hugging Face hand-overs are queries with receipts."
        raise ValueError(error_message)

    def query(self, message: JsonObject) -> dict[str, Any]:
        """Answer one hand-over command with its receipt, read-back or access check.

        The ``command`` field selects the request; an unsupported command raises
        ``ValueError``.
        """
        assert self.queue is not None, "The host initializes modules first."
        command = message.get("command")
        if command == "stage_run_files":
            return {"receipt": self._stage_run_files(message)}
        if command == "run_files_receipt":
            try:
                repository, run_id = run_reference(message)
            except RequestRefusedError as refusal:
                return {"receipt": _refused_run_files_receipt(message, refusal.code)}
            return {
                "receipt": {
                    "status": "recorded",
                    **self.queue.run_receipt(repository, run_id),
                }
            }
        if command == "read_back_run_files":
            return {"read_back": self._read_back_run_files(message)}
        if command == "stage_dataset_bundle":
            return {"receipt": self._stage_dataset_bundle(message)}
        if command == "handover_receipt":
            return {"receipt": self._handover_receipt(message)}
        if command == "collector_submission_receipt":
            return {"receipt": self._collector_submission_receipt(message)}
        if command == "check_write_access":
            return {"access": self._check_write_access(message)}
        if command in {"publish_release", "promote_build", "withdraw_build"}:
            return {"receipt": self._release_change(message)}
        error_message = "unsupported Hugging Face hand-over command"
        raise ValueError(error_message)

    def _stage_dataset_bundle(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        try:
            hand_over = parse_stage_dataset_bundle(message)
        except RequestRefusedError as refusal:
            repository = message.get("repository")
            return {
                "handover_id": "",
                "status": "refused",
                "error_code": refusal.code,
                "revision": None,
                "attempts": 0,
                "repository": repository if isinstance(repository, str) else "",
                "prefix": "",
                "copied_files": [],
            }
        receipt = self.queue.enqueue_job(
            handover_id=hand_over.handover_id,
            kind=DATASET_BUNDLE,
            target=hand_over.repository,
            request=hand_over.request,
            receipt={
                "handover_id": hand_over.handover_id,
                "status": "pending",
                "error_code": None,
                "revision": None,
                "attempts": 0,
                "repository": hand_over.repository,
                "prefix": hand_over.remote_prefix,
                "copied_files": sorted(hand_over.copied),
            },
            now=self.clock(),
            files=hand_over.copied,
        )
        if self.collector_delivery is not None and wants_collector_submission(
            hand_over.request
        ):
            try:
                self.collector_delivery.enqueue_for_bundle(
                    hand_over.handover_id, hand_over.request, hand_over.copied
                )
            except Exception as error:
                # The Hugging Face row is queued: answer with its receipt.
                self.delivery.warn_once(
                    "collector queue",
                    COLLECTOR_QUEUE_ERROR_MESSAGE.format(
                        error_type=type(error).__name__
                    ),
                )
        return receipt

    def _caller(self) -> ReleaseCaller:
        return ReleaseCaller(user=_user_name(), parent_pid=self.context.parent_pid)

    def _release_change(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None

        def refused(
            code: str, handover_id: str = "", reason: str | None = None
        ) -> dict[str, Any]:
            return {
                "handover_id": handover_id,
                "status": "refused",
                "error_code": code,
                "message": reason,
                "commit": None,
                "repository": str(message.get("repository") or ""),
                "release": str(message.get("release") or ""),
                "build": str(message.get("build") or ""),
                "attempts": 0,
                "current": None,
            }

        try:
            hand_over = parse_release_change(message)
        except RequestRefusedError as refusal:
            return refused(refusal.code)
        # Who may change a release is decided here, when the change is handed
        # over by its caller; a refused change is never queued.
        reason = self.release_change_policy(hand_over.change, self._caller())
        if reason is not None:
            return refused("NOT_AUTHORIZED", hand_over.handover_id, str(reason))
        return self.queue.enqueue_job(
            handover_id=hand_over.handover_id,
            kind=RELEASE_CHANGE,
            target=hand_over.repository,
            request=hand_over.request,
            receipt={
                "handover_id": hand_over.handover_id,
                "status": "pending",
                "error_code": None,
                "message": None,
                "commit": None,
                "repository": hand_over.repository,
                "release": hand_over.change.release,
                "build": hand_over.change.build,
                "attempts": 0,
                "current": None,
            },
            now=self.clock(),
        )

    def _handover_receipt(self, message: JsonObject) -> dict[str, Any] | None:
        assert self.queue is not None
        require_supported_version(message)
        handover_id = message.get("handover_id")
        if not isinstance(handover_id, str) or not handover_id:
            error_message = "handover_id must be a nonempty string"
            raise ValueError(error_message)
        return self.queue.job_receipt(handover_id)

    def _collector_submission_receipt(
        self, message: JsonObject
    ) -> dict[str, Any] | None:
        require_supported_version(message)
        handover_id = message.get("handover_id")
        if not isinstance(handover_id, str) or not handover_id:
            error_message = "handover_id must be a nonempty string"
            raise ValueError(error_message)
        if self.collector_delivery is None:
            return None
        return self.collector_delivery.receipt(handover_id)

    def _check_write_access(self, message: JsonObject) -> dict[str, Any]:
        """Microcosm's staged-dataset preflight: credential, reach and scope."""
        require_supported_version(message)
        repository = repository_identifier(message.get("repository"))
        access: dict[str, Any] = {
            "status": "checked",
            "credential": "missing",
            "reachable": None,
            "can_write": None,
            "error_type": None,
        }
        api = self.api_factory()
        if api is None:
            return access
        hub = DatasetRepository(repository, api)
        access["credential"] = "present"
        try:
            hub.head_revision()
        except Exception as error:
            # Only the type name: the Hub's message can carry request details.
            return {**access, "reachable": False, "error_type": type(error).__name__}
        try:
            can_write = hub.credential_can_write()
        except Exception:
            can_write = None
        return {**access, "reachable": True, "can_write": can_write}

    def _stage_run_files(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        try:
            hand_over = parse_stage_run_files(message)
        except RequestRefusedError as refusal:
            return _refused_run_files_receipt(message, refusal.code)
        self.queue.stage_run_files(
            repository=hand_over.repository,
            run_id=hand_over.run_id,
            files=hand_over.files,
            run_index_entry=hand_over.run_index_entry,
            flush=hand_over.flush,
            interval=hand_over.interval,
            now=self.clock(),
        )
        return {
            "status": "recorded",
            **self.queue.run_receipt(hand_over.repository, hand_over.run_id),
        }

    def _read_back_run_files(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        result: dict[str, Any] = {
            "status": "refused",
            "files": 0,
            "mismatched_paths": [],
            "error_code": None,
        }
        try:
            repository, run_id = run_reference(message)
        except RequestRefusedError as refusal:
            return {**result, "error_code": refusal.code}
        if self.queue.run_receipt(repository, run_id)["pending_files"]:
            return {**result, "status": "pending"}
        committed = self.queue.committed_run_files(repository, run_id)
        if not committed:
            # A refused hand-over, or a run this database never held: passing
            # would claim a delivery that this emitter cannot show.
            return {**result, "status": "failed", "error_code": NO_COMMITTED_FILES}
        api = self.api_factory()
        if api is None:
            return {**result, "status": "failed", "error_code": CREDENTIAL_MISSING}
        hub = DatasetRepository(repository, api)
        mismatched = []
        try:
            for path_in_repo, digest in sorted(committed.items()):
                if sha256_bytes(hub.download(path_in_repo)) != digest:
                    mismatched.append(path_in_repo)
        except Exception:
            return {
                **result,
                "status": "failed",
                "files": len(committed),
                "error_code": "REPOSITORY_UNAVAILABLE",
            }
        return {
            "status": "failed" if mismatched else "passed",
            "files": len(committed),
            "mismatched_paths": mismatched,
            "error_code": None,
        }

    def tick(self, now: float) -> None:
        """Run one delivery cycle on the host's thread; an error prints a warning.

        With background workers it commits run files only; without them it runs
        every delivery, collector submissions included. An exception prints a
        one-time warning (``warn_once``) instead of stopping the module. ``now``
        is not used.
        """
        assert self.delivery is not None, "The host initializes modules first."
        try:
            if self.workers:
                # Uploads that can take minutes run on the module's workers.
                self.delivery.run_file_cycle()
            else:
                self.delivery.run_cycle()
        except Exception:
            # The host stops a module whose tick raises; a local queue error
            # leaves the work queued and must not end every later delivery.
            self._worker_error()
        if self.collector_delivery is not None and not self.workers:
            try:
                self.collector_delivery.run_cycle()
            except Exception:
                self._collector_error()

    def parent_exited(self) -> None:
        """Queued hand-overs stay eligible for the next emitter host."""

    def close(self, deadline: float) -> None:
        """Commit pending run files once more, then stop by ``deadline``.

        The commit at shutdown runs first, on the calling thread, for every
        repository a worker is not uploading to at that moment. Once stopped,
        a worker starts no new upload or submission. A Hugging Face upload in
        progress has until ``deadline`` to finish, and that repository's run
        files are committed after it if time remains. Only then does the
        module wait for a collector submission in progress, for at most
        ``COLLECTOR_SHUTDOWN_WAIT_SECONDS`` and never past ``deadline``, so
        collector work never uses up the time the Hugging Face commits need.
        An upload or submission still running at the end is abandoned when
        the process exits; its lease expires and the next emitter retries it.
        """
        if self._closed:
            return
        self._closed = True
        for worker in self.workers.values():
            worker.stop()
        hugging_face_workers = [
            worker
            for kind, worker in self.workers.items()
            if kind != COLLECTOR_SUBMISSION
        ]
        collector_worker = self.workers.get(COLLECTOR_SUBMISSION)
        try:
            self._drain(deadline)
            hugging_face_stopped = all(
                [worker.wait(deadline) for worker in hugging_face_workers]
            )
            if hugging_face_workers and hugging_face_stopped:
                self._drain(deadline)
            collector_stopped = collector_worker is None or collector_worker.wait(
                min(deadline, time.monotonic() + COLLECTOR_SHUTDOWN_WAIT_SECONDS)
            )
            workers_stopped = hugging_face_stopped and collector_stopped
        except Exception:
            self._worker_error()
            workers_stopped = False
        finally:
            # A worker still uploading needs its HTTP client, and the queue to
            # record its result; both go with the process.
            if not self.workers or workers_stopped:
                if self.collector_delivery is not None:
                    self.collector_delivery.client.close()
                if self.queue is not None:
                    self.queue.close()

    def _drain(self, deadline: float) -> None:
        if self.delivery is not None and time.monotonic() < deadline:
            self.delivery.drain(deadline)

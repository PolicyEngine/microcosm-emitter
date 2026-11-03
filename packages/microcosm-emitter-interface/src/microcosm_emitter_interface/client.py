"""One typed call per kind of hand-over from a build to its local emitter.

Starting the client starts the installed ``microcosm-emitter`` command as this
process's child. No call raises into the build: not when the emitter is missing,
slow or refusing, and not when an argument cannot be converted to JSON (for
example a multi-element NumPy array or a self-referencing mapping). Each call
converts its arguments inside its error handling, reports that it was not
accepted, records why in ``EmitterClient.last_failure``, and the build continues.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from microcosm_emitter_interface.bundles import (
    DatasetBundleReceipt,
    StageDatasetBundle,
    WriteAccess,
    unavailable_bundle_receipt,
    unavailable_write_access,
)
from microcosm_emitter_interface.identifiers import repository_identifier
from microcosm_emitter_interface.launcher import (
    EmitterProcess,
    EmitterStartupError,
    start_emitter,
)
from microcosm_emitter_interface.orrery import (
    OrrerySettings,
    PublicationFileRole,
    PublicationInventory,
    PublicationReceipt,
    answered_inventory,
    answered_receipt,
    enqueue_message,
    inventory_message,
    publication_id_of,
    receipt_message,
    retry_message,
    unavailable_receipt,
)
from microcosm_emitter_interface.protocol import (
    ACKNOWLEDGEMENT_TIMEOUT_SECONDS,
    FILE_REQUEST_TIMEOUT_SECONDS,
    HUGGING_FACE_MODULE,
    ORRERY_MODULE,
    PROTOCOL_VERSION,
    READ_BACK_TIMEOUT_SECONDS,
    SETTLED_STATUSES,
    STAGING_REQUEST_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    TELEMETRY_MODULE,
)
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseChange,
    ReleaseReceipt,
    WithdrawBuild,
    unavailable_release_receipt,
)
from microcosm_emitter_interface.spool import default_spool_path
from microcosm_emitter_interface.staging import (
    HuggingFaceSettings,
    ReadBackResult,
    RunFilesReceipt,
    StageRunFiles,
    run_reference_message,
    unavailable_read_back,
    unavailable_run_files_receipt,
)
from microcosm_emitter_interface.telemetry import (
    TelemetryCommand,
    TelemetryRecorder,
    TelemetrySettings,
    telemetry_message,
)

if TYPE_CHECKING:
    import os

    from microcosm_emitter_interface.submissions import CollectorSubmissionReceipt

# Each print in this module that suppresses Ruff's T201 writes one of its
# messages to standard error on purpose; none is leftover debugging output.

STARTUP_WARNING = (
    "Local Microcosm emitter failed to start ({reason}); the build will continue."
)
REQUEST_WARNING = (
    "The local Microcosm emitter did not accept a {kind} request "
    "({reason}, {error_type}); the build will continue."
)

type RequestFailureReason = Literal[
    # No emitter is running, or it did not load the call's module.
    "emitter_unavailable",
    # The call's arguments could not be converted to a protocol message.
    "invalid_arguments",
    # The emitter refused the request, or did not answer in time.
    "not_accepted",
]

type MessageBuilder = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class RequestFailure:
    """Why a call reported that the emitter did not accept it.

    ``error_type`` is only the exception's type name, never its message.
    """

    kind: str
    reason: RequestFailureReason
    error_type: str | None = None


class EmitterClient:
    """Typed hand-overs to one emitter process owned by this build."""

    def __init__(
        self,
        process: EmitterProcess | None = None,
        *,
        startup_error: EmitterStartupError | None = None,
    ) -> None:
        """Hold a started emitter process, or an unavailable client's startup error.

        ``EmitterClient.start`` is the usual way to build one. Without
        ``process`` the client is unavailable, and every call reports that it
        was not accepted.
        """
        self._process = process
        self.startup_error = startup_error
        self._closed = False
        self._warned: set[str] = set()
        self._warning_lock = threading.Lock()
        #: The most recent call that reported failure, and why.
        self.last_failure: RequestFailure | None = None
        self.telemetry = TelemetryRecorder(self)

    # The published interface's entry point: builds call it with these
    # keyword arguments, so its signature stays as released.
    @classmethod
    def start(  # noqa: PLR0913
        cls,
        *,
        telemetry: TelemetrySettings | None = None,
        orrery: OrrerySettings | None = None,
        hugging_face: HuggingFaceSettings | None = None,
        spool_path: Path | str | None = None,
        command: str | os.PathLike[str] | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> EmitterClient:
        """Start the selected modules; return an unavailable client on failure."""
        try:
            path = Path(spool_path) if spool_path is not None else default_spool_path()
            modules: dict[str, dict[str, Any]] = {}
            if telemetry is not None:
                modules[TELEMETRY_MODULE] = telemetry.configuration(path)
            if orrery is not None:
                modules[ORRERY_MODULE] = orrery.configuration(path)
            if hugging_face is not None:
                modules[HUGGING_FACE_MODULE] = hugging_face.configuration(path)
        # No call raises into the build: any failure gives an unavailable
        # client with a fixed startup reason.
        except Exception:  # noqa: BLE001
            failure = EmitterStartupError("invalid_configuration")
            print(STARTUP_WARNING.format(reason=failure.reason), file=sys.stderr)  # noqa: T201
            return cls(startup_error=failure)
        try:
            process = start_emitter(
                modules, command=command, startup_timeout=startup_timeout
            )
        except EmitterStartupError as error:
            print(STARTUP_WARNING.format(reason=error.reason), file=sys.stderr)  # noqa: T201
            return cls(startup_error=error)
        # No call raises into the build: any failure gives an unavailable
        # client with a fixed startup reason.
        except Exception:  # noqa: BLE001
            failure = EmitterStartupError("startup_failure")
            print(STARTUP_WARNING.format(reason=failure.reason), file=sys.stderr)  # noqa: T201
            return cls(startup_error=failure)
        return cls(process)

    @property
    def available(self) -> bool:
        """Whether the client has an emitter process and has not been closed."""
        return self._process is not None and not self._closed

    @property
    def modules(self) -> frozenset[str]:
        """The modules the running emitter loaded."""
        if not self.available:
            return frozenset()
        if self._process is None:
            raise AssertionError
        return frozenset(self._process.description.get("modules", ()))

    @property
    def emitter_version(self) -> str | None:
        """The version the emitter reported at startup, or None if there is none.

        It is None for a client without an emitter process, and when the
        emitter's ``hello`` answer had no string ``emitter_version``.
        """
        if self._process is None:
            return None
        version = self._process.description.get("emitter_version")
        return version if isinstance(version, str) else None

    def _fail(
        self,
        kind: str,
        reason: RequestFailureReason,
        error: BaseException | None = None,
    ) -> None:
        """Record why a call failed; warn once per kind of call.

        A missing emitter was already reported when it failed to start, so
        only refused requests and unconvertible arguments print a warning.
        """
        error_type = None if error is None else type(error).__name__
        self.last_failure = RequestFailure(kind, reason, error_type)
        if reason == "emitter_unavailable":
            return
        with self._warning_lock:
            if kind in self._warned:
                return
            self._warned.add(kind)
        print(  # noqa: T201
            REQUEST_WARNING.format(kind=kind, reason=reason, error_type=error_type),
            file=sys.stderr,
        )

    def _message(
        self, build_message: MessageBuilder, *, kind: str
    ) -> dict[str, Any] | None:
        """Return the converted message, or None after recording why it failed."""
        try:
            return dict(build_message())
        # No call raises into the build; the failure is reported in last_failure.
        except Exception as error:  # noqa: BLE001
            self._fail(kind, "invalid_arguments", error)
            return None

    def _send(self, module: str, build_message: MessageBuilder, *, kind: str) -> bool:
        if module not in self.modules:
            self._fail(kind, "emitter_unavailable")
            return False
        message = self._message(build_message, kind=kind)
        if message is None:
            return False
        if self._process is None:
            raise AssertionError
        try:
            self._process.transport.send(
                module, message, timeout=ACKNOWLEDGEMENT_TIMEOUT_SECONDS
            )
        # No call raises into the build; the failure is reported in last_failure.
        except Exception as error:  # noqa: BLE001
            self._fail(kind, "not_accepted", error)
            return False
        return True

    def _request(
        self,
        module: str,
        build_message: MessageBuilder,
        *,
        kind: str,
        timeout: float = FILE_REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[dict[str, Any] | None, RequestFailureReason | None]:
        """Return the module's answer, or None and why this call failed.

        The reason belongs to this call only. ``last_failure`` is shared by
        every call and thread, so a receipt's error code never reads it.
        """
        if module not in self.modules:
            self._fail(kind, "emitter_unavailable")
            return None, "emitter_unavailable"
        message = self._message(build_message, kind=kind)
        if message is None:
            return None, "invalid_arguments"
        if self._process is None:
            raise AssertionError
        try:
            return self._process.transport.query(module, message, timeout=timeout), None
        # No call raises into the build; the failure is reported in last_failure.
        except Exception as error:  # noqa: BLE001
            self._fail(kind, "not_accepted", error)
            return None, "not_accepted"

    def _query(
        self,
        module: str,
        build_message: MessageBuilder,
        *,
        kind: str,
        timeout: float = FILE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | None:
        answer, _ = self._request(module, build_message, kind=kind, timeout=timeout)
        return answer

    def emit_telemetry(self, command: TelemetryCommand) -> bool:
        """Hand over one telemetry event; True once the emitter committed it."""
        return self._send(
            TELEMETRY_MODULE, lambda: telemetry_message(command), kind="telemetry"
        )

    def orrery_inventory(
        self,
        directory: Path,
        roles: Mapping[str, PublicationFileRole],
        *,
        publication_id: str | None = None,
    ) -> PublicationInventory | None:
        """Ask the emitter to measure the selected preserved Orrery input files."""
        response = self._query(
            ORRERY_MODULE,
            lambda: inventory_message(directory, roles, publication_id),
            kind="Orrery",
        )
        return answered_inventory(response)

    def hand_over_orrery_inputs(
        self,
        directory: Path,
        inventory: PublicationInventory,
        *,
        wait_seconds: float = 30.0,
    ) -> PublicationReceipt:
        """Queue the inventoried files; wait only after the durable enqueue."""
        publication_id = publication_id_of(inventory)
        if publication_id is None:
            self._fail("Orrery", "invalid_arguments")
            return unavailable_receipt(None, error_code="invalid_arguments")
        receipt = self._orrery_receipt_request(
            lambda: enqueue_message(directory, inventory), publication_id
        )
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and not receipt.get("attempts", 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.orrery_receipt(publication_id)
        return receipt

    def orrery_receipt(self, publication_id: str) -> PublicationReceipt:
        """Return the emitter's latest delivery receipt for one publication.

        Without an answer that holds a receipt, the result is a ``failed``
        receipt whose ``error_code`` says why (``local_service_unavailable``
        or ``invalid_arguments``).
        """
        return self._orrery_receipt_request(
            lambda: receipt_message(publication_id), publication_id
        )

    def retry_orrery_inputs(
        self, publication_id: str, *, wait_seconds: float = 0
    ) -> PublicationReceipt:
        """Ask the emitter to attempt a pending publication's delivery again now.

        Waits up to ``wait_seconds`` (by default not at all) while the receipt
        stays ``pending`` with an unchanged ``attempts`` count, then returns
        the latest receipt.
        """
        receipt = self._orrery_receipt_request(
            lambda: retry_message(publication_id), publication_id
        )
        previous_attempts = receipt.get("attempts", 0)
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and receipt.get("attempts", 0) == previous_attempts
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.orrery_receipt(publication_id)
        return receipt

    def _orrery_receipt_request(
        self, build_message: MessageBuilder, publication_id: str | None
    ) -> PublicationReceipt:
        answer, failure = self._request(ORRERY_MODULE, build_message, kind="Orrery")
        return answered_receipt(
            answer, publication_id, error_code=_receipt_error_code(failure)
        )

    def stage_run_files(self, request: StageRunFiles) -> RunFilesReceipt:
        """Hand over a staging run's files; returns once they are queued.

        The emitter copies the files' bytes before answering, so the build
        may rewrite them immediately afterwards.
        """
        response = self._query(
            HUGGING_FACE_MODULE,
            request.message,
            kind="staging",
            timeout=STAGING_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        if isinstance(value, dict):
            return cast("RunFilesReceipt", value)
        return unavailable_run_files_receipt(request.repository, request.run_id)

    def run_files_receipt(self, repository: str, run_id: str) -> RunFilesReceipt:
        """Return the emitter's current receipt for one staging run's files.

        Without an answer, the receipt's ``status`` is ``unavailable``.
        """
        response = self._query(
            HUGGING_FACE_MODULE,
            lambda: run_reference_message("run_files_receipt", repository, run_id),
            kind="staging",
            timeout=STAGING_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        if isinstance(value, dict):
            return cast("RunFilesReceipt", value)
        return unavailable_run_files_receipt(repository, run_id)

    def read_back_run_files(
        self, repository: str, run_id: str, *, wait_seconds: float = 30.0
    ) -> ReadBackResult:
        """Commit the run's pending files now, then compare the Hub's copies.

        Waits up to ``wait_seconds`` for pending files to be committed; a run
        whose files are still pending then reads back as ``pending``.
        """
        if HUGGING_FACE_MODULE not in self.modules:
            self._fail("staging", "emitter_unavailable")
            return unavailable_read_back()
        try:
            flush = StageRunFiles(repository=repository, run_id=run_id, flush=True)
        # No call raises into the build; the failure is reported in last_failure.
        except Exception as error:  # noqa: BLE001
            self._fail("staging", "invalid_arguments", error)
            return unavailable_read_back()
        receipt = self.stage_run_files(flush)
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            receipt["status"] == "recorded"
            and receipt["pending_files"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.run_files_receipt(repository, run_id)
        response = self._query(
            HUGGING_FACE_MODULE,
            lambda: run_reference_message("read_back_run_files", repository, run_id),
            kind="staging",
            timeout=READ_BACK_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("read_back")
        if isinstance(value, dict):
            return cast("ReadBackResult", value)
        return unavailable_read_back()

    def _handover_receipt(self, handover_id: str) -> dict[str, Any] | None:
        response = self._query(
            HUGGING_FACE_MODULE,
            lambda: {
                "protocol_version": PROTOCOL_VERSION,
                "command": "handover_receipt",
                "handover_id": handover_id,
            },
            kind="staging",
            timeout=STAGING_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        return value if isinstance(value, dict) else None

    def _wait_until_settled(
        self, receipt: dict[str, Any], wait_seconds: float
    ) -> dict[str, Any]:
        handover_id = receipt.get("handover_id")
        if not isinstance(handover_id, str) or not handover_id:
            # Nothing to ask about: return the receipt as answered.
            return receipt
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            receipt.get("status") not in SETTLED_STATUSES
            and receipt.get("status") != "unavailable"
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            latest = self._handover_receipt(handover_id)
            if latest is None:
                break
            receipt = latest
        return receipt

    def stage_dataset_bundle(
        self, request: StageDatasetBundle, *, wait_seconds: float = 0
    ) -> DatasetBundleReceipt:
        """Queue a staged dataset bundle; optionally wait for its upload.

        Returns once the hand-over is durably queued (``pending``), or after
        it settles if that happens within ``wait_seconds``.
        """
        response = self._query(
            HUGGING_FACE_MODULE,
            request.message,
            kind="staging",
            timeout=FILE_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        if not isinstance(value, dict):
            return unavailable_bundle_receipt(request)
        return cast(
            "DatasetBundleReceipt", self._wait_until_settled(value, wait_seconds)
        )

    def dataset_bundle_receipt(self, handover_id: str) -> DatasetBundleReceipt | None:
        """Return a bundle hand-over's latest receipt, or None without an answer."""
        return cast("DatasetBundleReceipt | None", self._handover_receipt(handover_id))

    def collector_submission_receipt(
        self, handover_id: str
    ) -> CollectorSubmissionReceipt | None:
        """Return what the collector has made of a bundle's submissions so far.

        The emitter submits a bundle that carries ``CollectorProvenance`` after
        its Hugging Face commit, then checks each submission's status in the
        background until it is final. This call answers at once from the
        emitter's queue and never waits for either: a ``rejected`` or
        ``failed`` receipt carries the collector's reason. ``None`` when
        collector submissions are off, the bundle described no build or named
        no file the collector takes, or no emitter answered.
        """
        response = self._query(
            HUGGING_FACE_MODULE,
            lambda: {
                "protocol_version": PROTOCOL_VERSION,
                "command": "collector_submission_receipt",
                "handover_id": handover_id,
            },
            kind="collector_submission",
            timeout=STAGING_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        return (
            cast("CollectorSubmissionReceipt", value)
            if isinstance(value, dict)
            else None
        )

    def check_write_access(self, repository: str) -> WriteAccess:
        """Whether the build's credential can reach and write ``repository``."""
        response = self._query(
            HUGGING_FACE_MODULE,
            lambda: {
                "protocol_version": PROTOCOL_VERSION,
                "command": "check_write_access",
                "repository": repository_identifier(repository),
            },
            kind="staging",
            timeout=READ_BACK_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("access")
        if isinstance(value, dict):
            return cast("WriteAccess", value)
        return unavailable_write_access()

    def _release_change(
        self, request: ReleaseChange, wait_seconds: float
    ) -> ReleaseReceipt:
        response = self._query(
            HUGGING_FACE_MODULE,
            request.message,
            kind="release",
            timeout=FILE_REQUEST_TIMEOUT_SECONDS,
        )
        value = None if response is None else response.get("receipt")
        if not isinstance(value, dict):
            return unavailable_release_receipt(request)
        return cast("ReleaseReceipt", self._wait_until_settled(value, wait_seconds))

    def publish_release(
        self, request: PublishRelease, *, wait_seconds: float = 300.0
    ) -> ReleaseReceipt:
        """Queue a build's publication and wait for it to land or be refused.

        The emitter writes ``releases/<release>/<build>/`` and ``releases.json``
        in one commit. After ``wait_seconds`` a ``pending`` receipt means the
        emitter is still retrying; ask again with ``release_receipt``. A
        change still pending an hour after it was handed over is ``refused``
        with ``REQUEST_EXPIRED``.

        ``published`` means the build is listed. Whether it is current is in
        the receipt's ``current`` field, the release's current build after
        this change: with ``make_current=True``, the build became current
        exactly when ``receipt["current"] == request.build``. It does not when
        a change to ``current`` requested after this one landed first, or when
        the build was already published; ``message`` then says which.
        """
        return self._release_change(request, wait_seconds)

    def promote_build(
        self, request: PromoteBuild, *, wait_seconds: float = 60.0
    ) -> ReleaseReceipt:
        """Queue making a build current; wait up to ``wait_seconds`` for it to settle.

        A receipt still ``pending`` after the wait means the emitter is still
        retrying; ask again with ``release_receipt``.
        """
        return self._release_change(request, wait_seconds)

    def withdraw_build(
        self, request: WithdrawBuild, *, wait_seconds: float = 60.0
    ) -> ReleaseReceipt:
        """Queue a build's withdrawal; wait up to ``wait_seconds`` for it to settle.

        A receipt still ``pending`` after the wait means the emitter is still
        retrying; ask again with ``release_receipt``.
        """
        return self._release_change(request, wait_seconds)

    def release_receipt(self, handover_id: str) -> ReleaseReceipt | None:
        """Return a release change's latest receipt, or None without an answer."""
        return cast("ReleaseReceipt | None", self._handover_receipt(handover_id))

    def close(self) -> None:
        """Ask the emitter to drain and stop; repeated calls do nothing."""
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            try:
                self._process.close()
            # No call raises into the build; the failure is reported in
            # last_failure.
            except Exception as error:  # noqa: BLE001
                self._fail("close", "not_accepted", error)


def _receipt_error_code(failure: RequestFailureReason | None) -> str:
    """Return an unavailable receipt's error code, from its own call's failure.

    An answer without a receipt, or no answer, is ``local_service_unavailable``,
    as in the emitter's in-process client.
    """
    if failure == "invalid_arguments":
        return "invalid_arguments"
    return "local_service_unavailable"


def _wait_seconds(value: object) -> float:
    """Return ``value`` as seconds to wait, or 0.0 if it is not a positive number."""
    try:
        seconds = float(value)
    # No call raises into the build: a value float() cannot convert, whatever
    # it raises, waits no time.
    except Exception:  # noqa: BLE001
        return 0.0
    return seconds if seconds > 0 else 0.0

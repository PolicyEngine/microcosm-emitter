"""Submit one planned submission to the collector's artifact intake.

The steps follow the collector's submission contract
(PolicyEngine/microcosm-collector, ``docs/artifact-submissions.md``): exchange
the build's Hugging Face token for an ``artifacts:submit`` credential, create
the submission, upload each file the collector still lacks to its signed URL,
and complete it. Every step can be repeated: the collector names a submission
by its content, so a later attempt continues where an earlier one stopped.

Once completion is accepted, ``check_status`` asks for the submission's
status (``GET /v1/submissions/{id}``) and, once it is final, reads its
receipt (``GET /v1/submissions/{id}/receipt``).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

import httpx

from microcosm_emitter.core.constants import LOOPBACK_HOSTS
from microcosm_emitter.huggingface.service.hub import (
    PRESERVED_BYTES_CHANGED,
    PRESERVED_FILES_MISSING,
    UPLOAD_FAILED,
)
from microcosm_emitter.huggingface.service.queue import sha256_file
from microcosm_emitter.submissions.planning import PlannedSubmission

if TYPE_CHECKING:
    from microcosm_emitter.core.auth import SessionCredential

SUBMIT_SCOPE: Final = "artifacts:submit"
SUBMISSIONS_PATH: Final = "/v1/submissions"
#: Signed upload URLs point at Cloud Storage's XML API host.
STORAGE_HOST: Final = "storage.googleapis.com"
REQUEST_TIMEOUT_SECONDS: Final = 30.0
#: An upload may wait 60 seconds to connect and for the answer. The write
#: timeout applies to each send of one ``UPLOAD_CHUNK_BYTES`` piece to the
#: connection. Over HTTPS (every Cloud Storage upload) CPython's ``ssl``
#: writes a piece whole, under one timeout, so each 64 KiB piece must be
#: written within 60 seconds: an average of at least 1,093 bytes a second
#: (about 8.7 kbit/s) over each piece. Over plain HTTP (a loopback
#: collector) a send returns once the kernel takes any bytes, so it fails
#: only after 60 seconds in which no byte was accepted. Either way a stalled
#: upload fails within about a minute, and an upload at or above that rate
#: has no limit on its total time, however large the file: a signed upload
#: is one ``PUT`` that stores nothing unless it completes, so stopping it
#: early would only start it again from zero.
UPLOAD_TIMEOUT: Final = httpx.Timeout(60.0)
#: Small enough to keep that HTTPS minimum low; httpx reads files in pieces
#: of the same size.
UPLOAD_CHUNK_BYTES: Final = 64 * 1024
RETRYABLE_STATUSES: Final = frozenset({408, 429})
#: After the collector shows it has no intake, how long to wait before
#: exchanging again; no submission request goes to it meanwhile.
INTAKE_RETRY_SECONDS: Final = 3600.0

CREDENTIAL_MISSING: Final = "CREDENTIAL_MISSING"
COLLECTOR_INTAKE_NOT_ENABLED: Final = "COLLECTOR_INTAKE_NOT_ENABLED"
#: Completion, status or receipt found no such submission: create it again.
SUBMISSION_NOT_FOUND: Final = "SUBMISSION_NOT_FOUND"
#: A status or receipt request answered 403
#: ``submission_owned_by_another_user``: the token's Hugging Face user did not
#: create the submission. The submission keeps awaiting its final status with
#: this code, the only 403 that does.
OWNED_BY_ANOTHER_USER: Final = "COLLECTOR_REFUSED_403"
#: A final status the collector reports without a rejection code.
COLLECTOR_REJECTED: Final = "COLLECTOR_REJECTED"
#: The contract's submission statuses, and those that are final.
SUBMISSION_STATUSES: Final = frozenset(
    {"awaiting_upload", "queued", "processing", "succeeded", "rejected", "failed"}
)
FINAL_STATUSES: Final = frozenset({"succeeded", "rejected", "failed"})
#: The emitter's state for an accepted submission whose status is not final.
AWAITING_FINAL_STATUS: Final = "awaiting_final_status"
CREDENTIAL_REJECTED: Final = "CREDENTIAL_REJECTED"
COLLECTOR_UNAVAILABLE: Final = "COLLECTOR_UNAVAILABLE"
INVALID_RESPONSE: Final = "INVALID_RESPONSE"
UPLOAD_URL_REFUSED: Final = "UPLOAD_URL_REFUSED"
FILES_INCOMPLETE: Final = "FILES_INCOMPLETE"


def _byte_slices(content: bytes) -> Iterator[bytes]:
    for start in range(0, len(content), UPLOAD_CHUNK_BYTES):
        yield content[start : start + UPLOAD_CHUNK_BYTES]


@dataclass(frozen=True)
class SubmissionOutcome:
    """One attempt's result. ``pending`` is retried; the others are final.

    ``retry_after`` is how long to wait before the next attempt, when the
    collector says so (for example an hour while it has no intake).
    """

    status: Literal["accepted", "pending", "failed"]
    error_code: str | None = None
    submission_id: str | None = None
    collector_status: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class StatusCheckOutcome:
    """One status check of a submission whose completion was accepted.

    ``status`` is:

    - ``awaiting_final_status``: the collector has not reported a final
      status yet, or did not answer (``error_code`` says why);
    - ``succeeded``, ``rejected`` or ``failed``: the collector's final status,
      with its ``receipt``; for ``rejected`` and ``failed``, ``rejection`` is
      the collector's ``{code, message}`` and ``error_code`` its code;
    - ``failed`` without a receipt: the collector refused the request for
      good (``error_code`` says how), but never because the submission
      belongs to another Hugging Face user, which keeps it awaiting;
    - ``pending``: the collector has no such submission
      (``SUBMISSION_NOT_FOUND``), so it is created again.

    ``collector_status`` is the contract status last reported, if any.
    """

    status: Literal[
        "awaiting_final_status", "succeeded", "rejected", "failed", "pending"
    ]
    error_code: str | None = None
    collector_status: str | None = None
    rejection: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    retry_after: float | None = None


def canonical_sha256(value: Any) -> str:
    """Return the hex SHA-256 digest of ``value`` in the contract's canonical JSON.

    That JSON has its keys sorted, no whitespace and non-ASCII characters
    escaped.
    """
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def receipt_matches(receipt: Any, submission_id: str) -> bool:
    """Return whether a receipt is a valid final receipt for ``submission_id``.

    It must be a mapping with a final ``status``, that ``submission_id``, and a
    ``receipt_sha256`` of its other fields, as the contract derives it.
    """
    if not isinstance(receipt, Mapping):
        return False
    digest = receipt.get("receipt_sha256")
    return (
        receipt.get("submission_id") == submission_id
        and receipt.get("status") in FINAL_STATUSES
        and isinstance(digest, str)
        and digest
        == canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    )


def _json_object(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        document = response.json()
    except ValueError:
        return None
    return document if isinstance(document, Mapping) else None


def upload_url_allowed(url: str, *, allow_loopback: bool) -> bool:
    """Whether a signed URL may receive a build's files."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return False
    if parsed.scheme == "https" and parsed.hostname == STORAGE_HOST:
        return True
    return (
        allow_loopback
        and parsed.scheme in {"http", "https"}
        and parsed.hostname in LOOPBACK_HOSTS
    )


def instruction_host_matches(url: str, headers: Mapping[str, str]) -> bool:
    """Whether an instruction's ``Host`` header, if any, names its URL's host.

    The collector signs ``Host`` with the other headers, and the client sends
    every header as given, so a different ``Host`` would send the file to
    another server than the URL names.
    """
    hosts = [value for key, value in headers.items() if key.lower() == "host"]
    return all(value.lower() == urlsplit(url).netloc.lower() for value in hosts)


def _preserved_files_outcome(planned: PlannedSubmission) -> SubmissionOutcome | None:
    """Re-hash the files submitted from their paths; copied files are not read."""
    for file in planned.body["files"]:
        if file["name"] in planned.copied:
            continue
        path = Path(planned.local_paths[file["name"]])
        try:
            if not path.is_file():
                return SubmissionOutcome("pending", PRESERVED_FILES_MISSING)
            if path.stat().st_size != file["size"]:
                return SubmissionOutcome("failed", PRESERVED_BYTES_CHANGED)
            if sha256_file(path) != file["sha256"]:
                return SubmissionOutcome("failed", PRESERVED_BYTES_CHANGED)
        except OSError:
            return SubmissionOutcome("pending", PRESERVED_FILES_MISSING)
    return None


class _StepFailedError(Exception):
    def __init__(self, outcome: SubmissionOutcome) -> None:
        super().__init__(outcome.error_code)
        self.outcome = outcome


class CollectorSubmissionClient:
    """Deliver planned submissions to one collector origin."""

    def __init__(
        self,
        origin: str,
        *,
        credential: Callable[[], SessionCredential],
        invalidate_credential: Callable[[], None],
        http: httpx.Client | None = None,
        allow_loopback_uploads: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the collector origin, the credential callables and the HTTP client.

        Without ``http``, a new ``httpx.Client`` that does not follow redirects
        is made. ``allow_loopback_uploads`` also accepts signed upload URLs on a
        loopback host, for a development collector.
        """
        self.origin = origin.rstrip("/")
        self._credential = credential
        self._invalidate_credential = invalidate_credential
        self._http = http or httpx.Client(follow_redirects=False)
        self._allow_loopback_uploads = allow_loopback_uploads
        self._clock = clock
        self._intake_unavailable_until: float | None = None

    def _intake_unavailable(self) -> SubmissionOutcome:
        """Discard the token and send nothing to this collector for an hour."""
        self._invalidate_credential()
        self._intake_unavailable_until = self._clock() + INTAKE_RETRY_SECONDS
        return SubmissionOutcome(
            "pending", COLLECTOR_INTAKE_NOT_ENABLED, retry_after=INTAKE_RETRY_SECONDS
        )

    def _waiting_for_intake(self) -> SubmissionOutcome | None:
        until = self._intake_unavailable_until
        if until is None:
            return None
        remaining = until - self._clock()
        if remaining <= 0:
            self._intake_unavailable_until = None
            return None
        return SubmissionOutcome(
            "pending", COLLECTOR_INTAKE_NOT_ENABLED, retry_after=remaining
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def _token(self) -> str:
        result, token = self._credential()
        if result == "ok" and token:
            return token
        if result == "missing":
            raise _StepFailedError(SubmissionOutcome("pending", CREDENTIAL_MISSING))
        if result in ("rejected", "unsendable"):
            raise _StepFailedError(SubmissionOutcome("failed", CREDENTIAL_REJECTED))
        if result == "intake_unavailable":
            raise _StepFailedError(self._intake_unavailable())
        # Includes the 404 a collector answers while its intake is off.
        raise _StepFailedError(SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE))

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        """Return the string ``code`` of a JSON error body, or ``None``."""
        try:
            document = response.json()
        except ValueError:
            return None
        code = document.get("code") if isinstance(document, Mapping) else None
        return code if isinstance(code, str) else None

    def _settled_answer(
        self, response: httpx.Response, *, names_submission: bool
    ) -> SubmissionOutcome:
        """Return the outcome that a non-success answer means.

        ``names_submission`` is true for the requests about one submission
        (completion, status, receipt), where a 404 ``submission_not_found``
        means "create it again". Every other 404, including one to create,
        means the collector has no intake.
        """
        status = response.status_code
        if status == 404:
            if (
                names_submission
                and self._error_code(response) == "submission_not_found"
            ):
                return SubmissionOutcome("pending", SUBMISSION_NOT_FOUND)
            return self._intake_unavailable()
        if status == 401:
            self._invalidate_credential()
            return SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)
        if 400 <= status < 500 and status not in RETRYABLE_STATUSES:
            return SubmissionOutcome("failed", f"COLLECTOR_REFUSED_{status}")
        return SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)

    def _send(
        self, method: str, path: str, token: str, body: Any = None
    ) -> httpx.Response:
        try:
            return self._http.request(
                method,
                self.origin + path,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise _StepFailedError(
                SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)
            ) from None

    def _send_in_scope(
        self, method: str, path: str, token: str, body: Any = None
    ) -> tuple[httpx.Response, str]:
        """Send, and once more with a fresh token after a 403 without a code.

        A 403 without a ``code`` means the token's scope does not allow the
        request; a fresh ``artifacts:submit`` token is tried once, and a second
        such refusal is final. A 403 with a code (for example
        ``submission_owned_by_another_user``) is final at once.
        """
        response = self._send(method, path, token, body)
        if response.status_code == 403 and self._error_code(response) is None:
            self._invalidate_credential()
            token = self._token()
            response = self._send(method, path, token, body)
        return response, token

    def _uploads(self, response: httpx.Response, planned: PlannedSubmission) -> tuple:
        try:
            document = response.json()
        except ValueError:
            raise _StepFailedError(
                SubmissionOutcome("pending", INVALID_RESPONSE)
            ) from None
        submission_id = document.get("submission_id")
        uploads = document.get("uploads")
        if not isinstance(submission_id, str) or not isinstance(uploads, list):
            raise _StepFailedError(SubmissionOutcome("pending", INVALID_RESPONSE))
        for upload in uploads:
            if (
                not isinstance(upload, Mapping)
                or upload.get("name") not in planned.local_paths
                or upload.get("method") != "PUT"
                or not isinstance(upload.get("url"), str)
                or not isinstance(upload.get("headers"), Mapping)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in upload["headers"].items()
                )
            ):
                raise _StepFailedError(SubmissionOutcome("pending", INVALID_RESPONSE))
            if not upload_url_allowed(
                upload["url"], allow_loopback=self._allow_loopback_uploads
            ) or not instruction_host_matches(upload["url"], upload["headers"]):
                raise _StepFailedError(SubmissionOutcome("failed", UPLOAD_URL_REFUSED))
        return submission_id, uploads

    def _upload(self, upload: Mapping[str, Any], planned: PlannedSubmission) -> None:
        copied = planned.copied.get(upload["name"])
        path = Path(planned.local_paths[upload["name"]])
        try:
            size = len(copied) if copied is not None else path.stat().st_size
            headers = {**upload["headers"], "Content-Length": str(size)}
            if copied is not None:
                response = self._http.put(
                    upload["url"],
                    content=_byte_slices(copied),
                    headers=headers,
                    timeout=UPLOAD_TIMEOUT,
                )
            else:
                with path.open("rb") as stream:
                    blocks = iter(lambda: stream.read(UPLOAD_CHUNK_BYTES), b"")
                    response = self._http.put(
                        upload["url"],
                        content=blocks,
                        headers=headers,
                        timeout=UPLOAD_TIMEOUT,
                    )
        except (httpx.HTTPError, OSError):
            raise _StepFailedError(
                SubmissionOutcome("pending", UPLOAD_FAILED)
            ) from None
        # 412: the object exists, and the bucket never replaces one.
        if response.status_code not in {200, 201, 412}:
            raise _StepFailedError(SubmissionOutcome("pending", UPLOAD_FAILED))

    def submit(self, planned: PlannedSubmission) -> SubmissionOutcome:
        """Make one attempt; safe to repeat until the outcome is final."""
        preserved = _preserved_files_outcome(planned)
        if preserved is not None:
            return preserved
        waiting = self._waiting_for_intake()
        if waiting is not None:
            return waiting
        try:
            token = self._token()
            created, token = self._send_in_scope(
                "POST", SUBMISSIONS_PATH, token, planned.body
            )
            if created.status_code not in {200, 201}:
                return self._settled_answer(created, names_submission=False)
            submission_id, uploads = self._uploads(created, planned)
            for upload in uploads:
                self._upload(upload, planned)
            # The uploads can outlast the token. Ask the session again: it
            # exchanges for a new token only when this one is about to expire.
            token = self._token()
            completed, token = self._send_in_scope(
                "POST", f"{SUBMISSIONS_PATH}/{submission_id}/complete", token
            )
        except _StepFailedError as failure:
            return failure.outcome
        if completed.status_code == 409:
            # Something is not stored as declared: create again, upload again.
            return SubmissionOutcome("pending", FILES_INCOMPLETE, submission_id)
        if completed.status_code not in {200, 202}:
            answer = self._settled_answer(completed, names_submission=True)
            return SubmissionOutcome(
                answer.status,
                answer.error_code,
                submission_id,
                retry_after=answer.retry_after,
            )
        # 202 (queued or processing) or 200 (final, for example ``rejected``
        # with ``stored_object_mismatch``): either way the contract's next
        # step is to check the status, then read the receipt once final.
        document = _json_object(completed) or {}
        collector_status = document.get("status")
        if collector_status not in SUBMISSION_STATUSES:
            collector_status = None
        return SubmissionOutcome(
            "accepted", submission_id=submission_id, collector_status=collector_status
        )

    def check_status(self, submission_id: str) -> StatusCheckOutcome:
        """Ask for an accepted submission's status; read its receipt once final.

        Reads no file, so it works after the build's files are gone. Safe to
        repeat: both requests only read.
        """
        waiting = self._waiting_for_intake()
        if waiting is not None:
            return StatusCheckOutcome(
                AWAITING_FINAL_STATUS,
                waiting.error_code,
                retry_after=waiting.retry_after,
            )
        path = f"{SUBMISSIONS_PATH}/{submission_id}"
        collector_status: str | None = None
        try:
            token = self._token()
            answered, token = self._send_in_scope("GET", path, token)
            if answered.status_code != 200:
                return self._unanswered_check(answered, None)
            status = (_json_object(answered) or {}).get("status")
            if status not in SUBMISSION_STATUSES:
                return StatusCheckOutcome(AWAITING_FINAL_STATUS, INVALID_RESPONSE)
            collector_status = str(status)
            if collector_status not in FINAL_STATUSES:
                return StatusCheckOutcome(
                    AWAITING_FINAL_STATUS, collector_status=collector_status
                )
            receipt_answer, token = self._send_in_scope("GET", f"{path}/receipt", token)
        except _StepFailedError as failure:
            # No answer, or no credential: keep checking. A refused Hugging
            # Face token is final only for submitting, never for the result.
            return StatusCheckOutcome(
                AWAITING_FINAL_STATUS,
                failure.outcome.error_code,
                collector_status,
                retry_after=failure.outcome.retry_after,
            )
        if receipt_answer.status_code == 409:
            # ``submission_not_final``: reprocessing returned it to ``queued``.
            status = (_json_object(receipt_answer) or {}).get("status")
            return StatusCheckOutcome(
                AWAITING_FINAL_STATUS,
                collector_status=str(status)
                if status in SUBMISSION_STATUSES
                else collector_status,
            )
        if receipt_answer.status_code != 200:
            return self._unanswered_check(receipt_answer, collector_status)
        receipt = _json_object(receipt_answer)
        if receipt is None or not receipt_matches(receipt, submission_id):
            return StatusCheckOutcome(
                AWAITING_FINAL_STATUS, INVALID_RESPONSE, collector_status
            )
        final_status = receipt["status"]
        if final_status == "succeeded":
            return StatusCheckOutcome(
                "succeeded", collector_status=final_status, receipt=dict(receipt)
            )
        rejection = receipt.get("rejection")
        rejection = dict(rejection) if isinstance(rejection, Mapping) else None
        code = None if rejection is None else rejection.get("code")
        return StatusCheckOutcome(
            final_status,
            code if isinstance(code, str) and code else COLLECTOR_REJECTED,
            final_status,
            rejection,
            dict(receipt),
        )

    def _unanswered_check(
        self, response: httpx.Response, collector_status: str | None
    ) -> StatusCheckOutcome:
        """Return what an error answer to a status or receipt request means."""
        answer = self._settled_answer(response, names_submission=True)
        if answer.error_code == SUBMISSION_NOT_FOUND:
            return StatusCheckOutcome("pending", SUBMISSION_NOT_FOUND)
        if (
            response.status_code == 403
            and self._error_code(response) == "submission_owned_by_another_user"
        ):
            # The collector has the submission, and only this token's Hugging
            # Face user cannot read it (another emitter on the machine may run
            # with the user's token), so checks go on, as for a refused
            # Hugging Face token.
            return StatusCheckOutcome(
                AWAITING_FINAL_STATUS, OWNED_BY_ANOTHER_USER, collector_status
            )
        if answer.status == "failed":
            # A fresh token refused by a 403 without a code, a 403 with
            # another code, 413 or 422.
            return StatusCheckOutcome("failed", answer.error_code, collector_status)
        return StatusCheckOutcome(
            AWAITING_FINAL_STATUS,
            answer.error_code,
            collector_status,
            retry_after=answer.retry_after,
        )

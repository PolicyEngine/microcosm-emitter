"""Submit one planned submission to the collector's artifact intake.

The steps follow the collector's submission contract: exchange the build's
Hugging Face token for an ``artifacts:submit`` credential, create the
submission, upload each file the collector still lacks to its signed URL, and
complete it. Every step can be repeated: the collector names a submission by
its content, so a later attempt continues where an earlier one stopped.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
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
#: A final status the collector reports without a rejection code.
COLLECTOR_REJECTED: Final = "COLLECTOR_REJECTED"
#: Final statuses that mean the submission will not succeed.
UNSUCCESSFUL_STATUSES: Final = frozenset({"rejected", "failed"})
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
        credential: Callable[[], tuple[str, str | None]],
        invalidate_credential: Callable[[], None],
        http: httpx.Client | None = None,
        allow_loopback_uploads: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
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
        self._http.close()

    def _token(self) -> str:
        result, token = self._credential()
        if result == "ok" and token:
            return token
        if result == "missing":
            raise _StepFailedError(SubmissionOutcome("pending", CREDENTIAL_MISSING))
        if result == "rejected":
            raise _StepFailedError(SubmissionOutcome("failed", CREDENTIAL_REJECTED))
        if result == "intake_unavailable":
            raise _StepFailedError(self._intake_unavailable())
        # Includes the 404 a collector answers while its intake is off.
        raise _StepFailedError(SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE))

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        """The ``code`` of an error body, if it has one."""
        try:
            document = response.json()
        except ValueError:
            return None
        code = document.get("code") if isinstance(document, Mapping) else None
        return code if isinstance(code, str) else None

    def _settled_answer(
        self, response: httpx.Response, *, names_submission: bool
    ) -> SubmissionOutcome:
        """What a non-success answer means.

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

    def _post(self, path: str, token: str, body: Any = None) -> httpx.Response:
        try:
            return self._http.post(
                self.origin + path,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise _StepFailedError(
                SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)
            ) from None

    def _post_in_scope(
        self, path: str, token: str, body: Any = None
    ) -> tuple[httpx.Response, str]:
        """Post, and once more with a fresh token after a 403 without a code.

        A 403 without a ``code`` means the token's scope does not allow the
        request; a fresh ``artifacts:submit`` token is tried once, and a second
        such refusal is final. A 403 with a code (for example
        ``submission_owned_by_another_user``) is final at once.
        """
        response = self._post(path, token, body)
        if response.status_code == 403 and self._error_code(response) is None:
            self._invalidate_credential()
            token = self._token()
            response = self._post(path, token, body)
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
            created, token = self._post_in_scope(SUBMISSIONS_PATH, token, planned.body)
            if created.status_code not in {200, 201}:
                return self._settled_answer(created, names_submission=False)
            submission_id, uploads = self._uploads(created, planned)
            for upload in uploads:
                self._upload(upload, planned)
            # The uploads can outlast the token. Ask the session again: it
            # exchanges for a new token only when this one is about to expire.
            token = self._token()
            completed, token = self._post_in_scope(
                f"{SUBMISSIONS_PATH}/{submission_id}/complete", token
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
        try:
            document = completed.json()
        except ValueError:
            document = None
        if not isinstance(document, Mapping):
            document = {}
        collector_status = document.get("status")
        if not isinstance(collector_status, str):
            collector_status = None
        if collector_status in UNSUCCESSFUL_STATUSES:
            # For example ``stored_object_mismatch``: stored objects are never
            # replaced, so repeating the submission cannot succeed.
            rejection = document.get("rejection")
            code = rejection.get("code") if isinstance(rejection, Mapping) else None
            return SubmissionOutcome(
                "failed",
                code if isinstance(code, str) and code else COLLECTOR_REJECTED,
                submission_id,
                collector_status,
            )
        return SubmissionOutcome(
            "accepted", submission_id=submission_id, collector_status=collector_status
        )

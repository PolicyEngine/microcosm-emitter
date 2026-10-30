"""Submitting one planned submission, checking its status and fetching its receipt.

Submitting is the exchange, create, upload and complete requests; the status is
then checked until it is final, and the receipt is fetched.
"""

import hashlib
import socket
import threading
import time

import httpx
import pytest
from fake_collector import FakeCollector

from microcosm_emitter.core.auth import CollectorSession
from microcosm_emitter.submissions.client import (
    SUBMIT_SCOPE,
    CollectorSubmissionClient,
    upload_url_allowed,
)
from microcosm_emitter.submissions.planning import PlannedSubmission

DIAGNOSTICS = b'{"schema_version": 8}'
MANIFEST = b'{"build": "uk-frs-calibration-attempt-1"}'


def planned_submission(directory, *, diagnostics=DIAGNOSTICS) -> PlannedSubmission:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "calibration_diagnostics.json").write_bytes(diagnostics)
    (directory / "build_manifest.json").write_bytes(MANIFEST)
    files = [
        ("build_manifest.json", "build_manifest", MANIFEST),
        ("calibration_diagnostics.json", "calibration_diagnostics", DIAGNOSTICS),
    ]
    return PlannedSubmission(
        kind="calibration-diagnostics",
        body={
            "contract_version": 1,
            "kind": "calibration-diagnostics",
            "licensed_data": True,
            "provenance": {
                "run_id": "uk-run-1",
                "build_id": "uk-run-1",
                "country_code": "UK",
                "release": None,
                "huggingface": {
                    "repo": "policyengine/populace-uk-staging",
                    "commit": "0" * 40,
                    "folder": "staged/uk-run-1",
                },
            },
            "files": [
                {
                    "name": name,
                    "role": role,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, role, content in files
            ],
        },
        local_paths={name: str(directory / name) for name, _, _ in files},
    )


class Invalidations:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def submission_client(
    collector, invalidations=None, clock=None, hugging_face_credential="hf_synthetic"
) -> CollectorSubmissionClient:
    session = CollectorSession(
        collector.origin,
        credential=lambda: hugging_face_credential,
        scope=SUBMIT_SCOPE,
    )
    return CollectorSubmissionClient(
        collector.origin,
        credential=session.credential,
        invalidate_credential=invalidations or session.invalidate,
        allow_loopback_uploads=True,
        clock=clock or ManualClock(),
    )


def test_a_submission_is_created_uploaded_and_completed(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "accepted"
    assert outcome.collector_status == "queued"
    assert outcome.submission_id in collector.submissions
    assert collector.exchange_requests == [{"scope": "artifacts:submit"}]
    assert sorted(collector.objects.values()) == sorted([DIAGNOSTICS, MANIFEST])
    assert collector.completed == [outcome.submission_id]


def test_files_already_stored_are_not_uploaded_again(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)
    client = submission_client(collector)
    client.submit(planned)
    uploads_before = list(collector.uploads)

    outcome = client.submit(planned)

    assert outcome.status == "accepted"
    assert collector.uploads == uploads_before


def test_an_upload_refused_as_already_stored_counts_as_stored(
    collector, tmp_path
) -> None:
    planned = planned_submission(tmp_path)
    client = submission_client(collector)
    collector.complete_statuses = [503]
    client.submit(planned)
    collector.sign_stored_files = True

    outcome = client.submit(planned)

    assert outcome.status == "accepted"


@pytest.mark.parametrize("intake", ["off", "absent"])
def test_a_collector_without_intake_leaves_the_submission_pending(
    collector, tmp_path, intake
) -> None:
    """A collector with intake off or absent leaves the submission pending.

    Off: the exchange answers 404 ``intake_not_enabled``. Absent: a collector
    released before intake answers 200 with a ``telemetry:write`` token.
    """
    collector.intake = intake

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "COLLECTOR_INTAKE_NOT_ENABLED"
    assert outcome.retry_after == pytest.approx(3600)
    assert collector.created == []


def test_without_intake_no_request_is_sent_until_the_hour_has_passed(
    collector, tmp_path
) -> None:
    collector.intake = "absent"
    clock = ManualClock()
    client = submission_client(collector, clock=clock)
    planned = planned_submission(tmp_path)
    client.submit(planned)
    exchanges = len(collector.exchange_requests)

    clock.now += 3599
    waiting = client.submit(planned)
    assert (waiting.status, waiting.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )
    assert waiting.retry_after == pytest.approx(1)
    assert len(collector.exchange_requests) == exchanges

    collector.intake = "on"
    clock.now += 1
    assert client.submit(planned).status == "accepted"
    assert collector.exchange_requests[-1] == {"scope": "artifacts:submit"}


def test_a_token_without_the_submit_scope_is_never_used_for_submissions():
    session = CollectorSession(
        "https://collector.test",
        post=lambda url, payload, token: (
            200,
            {
                "access_token": "telemetry",
                "expires_in": 900,
                "scope": "telemetry:write",
            },
        ),
        credential=lambda: "hf",
        scope=SUBMIT_SCOPE,
    )
    assert session.credential() == ("intake_unavailable", None)


@pytest.mark.parametrize(
    "hugging_face_credential",
    ["hf_synthetic", "hf_synthetic with a pasted space"],
    ids=["refused by the collector", "not sendable as a bearer token"],
)
def test_a_rejected_credential_fails_the_submission(
    collector, tmp_path, hugging_face_credential
) -> None:
    collector.exchange_status = 403

    outcome = submission_client(
        collector, hugging_face_credential=hugging_face_credential
    ).submit(planned_submission(tmp_path))

    assert outcome.status == "failed"
    assert outcome.error_code == "CREDENTIAL_REJECTED"
    # An unsendable credential is never sent.
    sendable = hugging_face_credential == "hf_synthetic"
    assert collector.exchange_credentials == (["hf_synthetic"] if sendable else [])


@pytest.mark.parametrize(
    "answer", [(403, "submission_owned_by_another_user"), 413, 422]
)
def test_a_submission_the_collector_refuses_fails(collector, tmp_path, answer) -> None:
    collector.create_statuses = [answer]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    status = answer[0] if isinstance(answer, tuple) else answer
    assert outcome.status == "failed"
    assert outcome.error_code == f"COLLECTOR_REFUSED_{status}"
    assert len(collector.exchange_requests) == 1


@pytest.mark.parametrize("step", ["create", "complete"])
def test_a_403_without_a_code_exchanges_again_for_the_submit_scope(
    collector, tmp_path, step
) -> None:
    """The token's scope did not allow the request; a fresh token does."""
    setattr(collector, f"{step}_statuses", [403])

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "accepted"
    assert collector.exchange_requests == [{"scope": "artifacts:submit"}] * 2


def test_a_fresh_token_refused_the_same_way_fails_the_submission(
    collector, tmp_path
) -> None:
    collector.create_statuses = [403, 403]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code) == ("failed", "COLLECTOR_REFUSED_403")
    assert len(collector.exchange_requests) == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_collector_outage_leaves_the_submission_pending(
    collector, tmp_path, status
) -> None:
    collector.create_statuses = [status]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "COLLECTOR_UNAVAILABLE"


def test_an_expired_session_is_exchanged_again(collector, tmp_path) -> None:
    collector.create_statuses = [401]
    invalidations = Invalidations()

    outcome = submission_client(collector, invalidations).submit(
        planned_submission(tmp_path)
    )

    assert outcome.status == "pending"
    assert invalidations.count == 1


def test_files_missing_at_completion_are_retried(collector, tmp_path) -> None:
    collector.complete_statuses = [409]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "FILES_INCOMPLETE"


def test_changed_preserved_bytes_fail_before_contacting_the_collector(
    collector, tmp_path
) -> None:
    planned = planned_submission(tmp_path)
    (tmp_path / "calibration_diagnostics.json").write_bytes(b'{"schema_version": 9}')

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "failed"
    assert outcome.error_code == "PRESERVED_BYTES_CHANGED"
    assert collector.exchange_requests == []


def test_missing_preserved_files_stay_pending(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)
    (tmp_path / "build_manifest.json").unlink()

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "pending"
    assert outcome.error_code == "PRESERVED_FILES_MISSING"


def test_uploads_go_only_to_cloud_storage_or_an_allowed_loopback(
    collector, tmp_path
) -> None:
    collector.upload_origin = "http://192.0.2.10"

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "failed"
    assert outcome.error_code == "UPLOAD_URL_REFUSED"
    assert collector.uploads == []


@pytest.mark.parametrize(
    ("url", "allow_loopback", "allowed"),
    [
        (
            "https://storage.googleapis.com/bucket/object?X-Goog-Signature=1",
            False,
            True,
        ),
        ("http://storage.googleapis.com/bucket/object", False, False),
        ("https://storage.googleapis.com.example.net/object", False, False),
        ("https://user@storage.googleapis.com/object", False, False),
        ("http://127.0.0.1:8080/storage/object", False, False),
        ("http://127.0.0.1:8080/storage/object", True, True),
    ],
)
def test_upload_url_allowed(url: str, *, allow_loopback: bool, allowed: bool) -> None:
    assert upload_url_allowed(url, allow_loopback=allow_loopback) is allowed


def test_a_session_asks_for_its_scope_and_telemetry_sessions_still_send_nothing():
    exchanged: list[dict] = []

    def record_post(url, payload, token):
        exchanged.append(payload)
        return 200, {
            "access_token": "token",
            "expires_in": 900,
            **({"scope": payload["scope"]} if "scope" in payload else {}),
        }

    CollectorSession(
        "https://collector.test",
        post=record_post,
        credential=lambda: "hf",
    ).credential()
    CollectorSession(
        "https://collector.test",
        post=record_post,
        credential=lambda: "hf",
        scope=SUBMIT_SCOPE,
    ).credential()

    assert exchanged == [{}, {"scope": "artifacts:submit"}]


@pytest.mark.parametrize(
    "answer",
    [404, (404, "intake_not_enabled"), (404, "submission_not_found")],
    ids=["no-code", "intake-not-enabled", "submission-not-found"],
)
def test_a_404_to_create_means_intake_is_not_available(
    collector, tmp_path, answer
) -> None:
    """Only completion, status and receipt may say a submission is unknown."""
    collector.create_statuses = [answer]
    invalidations = Invalidations()

    outcome = submission_client(collector, invalidations).submit(
        planned_submission(tmp_path)
    )

    assert (outcome.status, outcome.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )
    assert outcome.retry_after == pytest.approx(3600)
    assert invalidations.count == 1


def test_a_404_to_completion_without_its_code_means_intake_is_not_available(
    collector, tmp_path
) -> None:
    collector.complete_statuses = [404]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )


def test_an_unknown_submission_at_completion_is_created_again(
    collector, tmp_path
) -> None:
    """For example after the collector's database was reset."""
    collector.complete_statuses = [(404, "submission_not_found")]
    client = submission_client(collector)
    planned = planned_submission(tmp_path)

    first = client.submit(planned)
    second = client.submit(planned)

    assert (first.status, first.error_code) == ("pending", "SUBMISSION_NOT_FOUND")
    assert first.retry_after is None
    assert second.status == "accepted"
    assert len(collector.created) == 2


def test_a_stored_object_with_another_digest_is_rejected_with_a_receipt(
    collector, tmp_path
) -> None:
    """Completion answers 200 ``rejected`` with ``stored_object_mismatch``.

    The contract still says to poll the status, then read the receipt.
    """
    collector.corrupt_uploads = True
    client = submission_client(collector)

    outcome = client.submit(planned_submission(tmp_path))
    checked = client.check_status(outcome.submission_id)

    assert (outcome.status, outcome.collector_status) == ("accepted", "rejected")
    assert (checked.status, checked.error_code) == (
        "rejected",
        "stored_object_mismatch",
    )
    assert checked.rejection["code"] == "stored_object_mismatch"
    # Rejected at completion: the submission never had a job.
    assert checked.receipt["job"] is None


@pytest.mark.parametrize(
    ("final_status", "code"),
    [("rejected", "content_mismatch"), ("failed", "worker_error")],
)
def test_a_final_completion_answer_is_accepted_with_its_status(
    collector, tmp_path, final_status, code
) -> None:
    collector.complete_answers = [
        (
            200,
            {
                "contract_version": 1,
                "submission_id": "sub-chosen",
                "status": final_status,
                "rejection": {"code": code, "message": "chosen"},
            },
        )
    ]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    # Final for the collector; the status check then fetches the receipt.
    assert (outcome.status, outcome.error_code, outcome.collector_status) == (
        "accepted",
        None,
        final_status,
    )


def test_a_queued_completion_is_accepted_with_its_status(collector, tmp_path) -> None:
    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.collector_status) == ("accepted", "queued")
    document = collector._status_document(outcome.submission_id)
    assert document["job"]["processing_round"] == 1
    assert len(document["files_sha256"]) == 64


def test_uploads_send_the_instruction_host_header_as_given(collector, tmp_path) -> None:
    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "accepted"
    assert collector.upload_hosts == [collector.origin.split("://", 1)[1]] * 2


def test_an_instruction_whose_host_differs_from_its_url_is_refused(
    collector, tmp_path
) -> None:
    collector.upload_host = "storage.googleapis.com"

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code) == ("failed", "UPLOAD_URL_REFUSED")
    assert collector.uploads == []


# Slow and stalled uploads ---------------------------------------------------
#
# Both sides of the loopback connection get small socket buffers, so the
# client's sends wait for the fake collector to read the body.

SMALL_SOCKET_BUFFER_BYTES = 16 * 1024
#: Large enough to fill both small buffers many times over.
LARGE_FILE_BYTES = 5 * 1024 * 1024 // 2


@pytest.fixture
def slow_link_collector():
    fake = FakeCollector(receive_buffer_bytes=SMALL_SOCKET_BUFFER_BYTES).start()
    try:
        yield fake
    finally:
        fake.release_uploads()
        fake.stop()


def slow_link_client(collector) -> CollectorSubmissionClient:
    """Return a client with a small send buffer.

    With the fake's TLS uploads, it sends ``https://`` requests over the fake's
    Unix socket and trusts it.
    """
    session = CollectorSession(
        collector.origin, credential=lambda: "hf_synthetic", scope=SUBMIT_SCOPE
    )
    small_send_buffer = (socket.SOL_SOCKET, socket.SO_SNDBUF, SMALL_SOCKET_BUFFER_BYTES)
    tls_uploads = (
        {}
        if collector.upload_socket_path is None
        else {
            "https://": httpx.HTTPTransport(
                uds=collector.upload_socket_path,
                verify=collector.client_tls_context,
                socket_options=[small_send_buffer],
            )
        }
    )
    return CollectorSubmissionClient(
        collector.origin,
        credential=session.credential,
        invalidate_credential=session.invalidate,
        http=httpx.Client(
            follow_redirects=False,
            transport=httpx.HTTPTransport(socket_options=[small_send_buffer]),
            mounts=tls_uploads,
        ),
        allow_loopback_uploads=True,
        clock=ManualClock(),
    )


def one_large_file_submission(directory, content: bytes) -> PlannedSubmission:
    """Return a submission of one file holding ``content``, uploaded from its path."""
    template = planned_submission(directory / "template")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "calibration_diagnostics.json").write_bytes(content)
    files = [
        {
            "name": "calibration_diagnostics.json",
            "role": "calibration_diagnostics",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    return PlannedSubmission(
        kind=template.kind,
        body={**template.body, "files": files},
        local_paths={
            "calibration_diagnostics.json": str(
                directory / "calibration_diagnostics.json"
            )
        },
    )


def test_an_upload_that_keeps_progressing_is_never_stopped(
    slow_link_collector, tmp_path, monkeypatch
) -> None:
    """However long it takes, an upload that keeps making progress completes.

    The fake reads 64 KiB every 20 ms, so the 2.5 MiB file takes about a
    second, and each send makes progress well within the 1-second stall
    threshold set here. Any limit on an upload's total time derived from
    the file's size is set as short as the client allows it: the client
    before this fix stopped every upload after such a limit.
    """
    from microcosm_emitter.submissions import client as client_module

    monkeypatch.setattr(client_module, "UPLOAD_TIMEOUT", httpx.Timeout(10.0, write=1.0))
    monkeypatch.setattr(client_module, "MIN_UPLOAD_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(
        client_module, "MIN_UPLOAD_BYTES_PER_SECOND", 1 << 40, raising=False
    )
    slow_link_collector.upload_read_pace = (64 * 1024, 0.02)
    content = b"x" * LARGE_FILE_BYTES
    planned = one_large_file_submission(tmp_path, content)

    outcome = slow_link_client(slow_link_collector).submit(planned)

    assert (outcome.status, outcome.error_code) == ("accepted", None), (
        "an upload that kept making progress was stopped"
    )
    assert list(slow_link_collector.objects.values()) == [content]


def test_a_stalled_upload_is_abandoned(
    slow_link_collector, tmp_path, monkeypatch
) -> None:
    """The fake reads nothing: once the buffers are full no send makes progress.

    The upload then fails after the stall threshold (0.5 s here).
    """
    from microcosm_emitter.submissions import client as client_module

    monkeypatch.setattr(client_module, "UPLOAD_TIMEOUT", httpx.Timeout(10.0, write=0.5))
    slow_link_collector.hold_uploads()
    planned = one_large_file_submission(tmp_path, b"x" * LARGE_FILE_BYTES)
    started = time.monotonic()

    outcome = slow_link_client(slow_link_collector).submit(planned)

    assert (outcome.status, outcome.error_code) == ("pending", "UPLOAD_FAILED")
    assert time.monotonic() - started < 5
    assert slow_link_collector.objects == {}


# Over HTTPS, as every production upload to Cloud Storage is, a send to the
# connection writes its whole buffer before it returns: CPython's ``ssl``
# does no partial writes, and its write timeout covers the whole call. So the
# write timeout covers each piece of the file the client sends, and the
# pieces must be small enough that a slow link still finishes each one well
# within it.

#: About 1 MiB/s: a 64 KiB piece takes about 60 ms, a 1 MiB piece a second.
TLS_READ_PACE = (16 * 1024, 1 / 64)
#: The stall threshold set here, in place of the client's 60 seconds.
TLS_WRITE_TIMEOUT_SECONDS = 0.5
#: More than one 1 MiB piece, so the client sends at least one whole piece.
TLS_FILE_BYTES = 3 * 1024 * 1024 // 2


def test_an_https_upload_that_keeps_progressing_is_never_stopped(
    tmp_path, monkeypatch
) -> None:
    """An HTTPS upload that keeps making progress is never stopped.

    The #27 round-2 review's NEW BUGS 1: the link averages about 1 MiB/s, so the
    upload keeps making progress, yet a 1 MiB piece takes twice the stall
    threshold to write. Every piece must finish within the threshold for the
    upload to complete.
    """
    from microcosm_emitter.submissions import client as client_module

    monkeypatch.setattr(
        client_module,
        "UPLOAD_TIMEOUT",
        httpx.Timeout(10.0, write=TLS_WRITE_TIMEOUT_SECONDS),
    )
    collector = FakeCollector(
        receive_buffer_bytes=SMALL_SOCKET_BUFFER_BYTES, tls_uploads=True
    ).start()
    try:
        collector.upload_read_pace = TLS_READ_PACE
        content = b"x" * TLS_FILE_BYTES
        planned = one_large_file_submission(tmp_path, content)

        outcome = slow_link_client(collector).submit(planned)

        assert (outcome.status, outcome.error_code) == ("accepted", None), (
            "an HTTPS upload that kept making progress was stopped"
        )
        assert list(collector.objects.values()) == [content]
        assert collector.upload_origin.startswith("https://")
    finally:
        collector.release_uploads()
        collector.stop()


def test_completion_after_uploads_that_outlast_the_token_uses_a_fresh_token(
    collector, tmp_path, monkeypatch
) -> None:
    """Completion after uploads that outlast the token uses a fresh token.

    The #27 round-2 review's CODE QUALITY 3: the uploads outlast the collector
    token, so completing with the token fetched before them is answered 401. The
    client must ask its session again just before it completes; the session
    exchanges again only when the token is expiring.
    """
    from microcosm_emitter.core import auth as auth_module

    monkeypatch.setattr(auth_module, "MINIMUM_TOKEN_LIFETIME_SECONDS", 1)
    monkeypatch.setattr(auth_module, "TOKEN_REFRESH_MARGIN_SECONDS", 0)
    collector.token_lifetime_seconds = 1
    collector.hold_uploads()
    planned = planned_submission(tmp_path)
    client = submission_client(collector)
    outcomes = []
    submitting = threading.Thread(
        target=lambda: outcomes.append(client.submit(planned)), daemon=True
    )
    submitting.start()
    try:
        assert collector.upload_started.wait(10), "no upload started"
        # The token expires while the upload is held.
        time.sleep(1.5)
    finally:
        collector.release_uploads()
        submitting.join(timeout=30)

    (outcome,) = outcomes
    assert (outcome.status, outcome.error_code) == ("accepted", None), (
        "the completion after a long upload used an expired token"
    )
    assert collector.exchange_requests == [{"scope": "artifacts:submit"}] * 2


# Checking an accepted submission's status, and fetching its receipt ---------


def accepted_submission(collector, tmp_path, **options):
    """Return a client, and the ID of a submission it created and completed."""
    client = submission_client(collector, **options)
    outcome = client.submit(planned_submission(tmp_path))
    assert (outcome.status, outcome.collector_status) == ("accepted", "queued")
    return client, outcome.submission_id


@pytest.mark.parametrize("collector_status", ["queued", "processing"])
def test_a_submission_not_final_yet_awaits_its_final_status(
    collector, tmp_path, collector_status
) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    if collector_status == "processing":
        collector.finish(submission_id, "processing")

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.collector_status) == (
        "awaiting_final_status",
        None,
        collector_status,
    )
    assert checked.receipt is None
    assert collector.status_requests == [submission_id]
    # The receipt is read only once the status is final.
    assert collector.receipt_requests == []


def test_a_succeeded_submission_brings_its_receipt(collector, tmp_path) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    result = {"processed_by": "intake-only", "verified_files": 2}
    collector.finish(submission_id, "succeeded", result=result)

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.collector_status) == (
        "succeeded",
        None,
        "succeeded",
    )
    assert checked.rejection is None
    assert checked.receipt == collector.receipt_document(submission_id)
    assert checked.receipt["result"] == result
    assert collector.status_requests == [submission_id]
    assert collector.receipt_requests == [submission_id]


@pytest.mark.parametrize(
    ("final_status", "code"),
    [
        ("rejected", "content_mismatch"),
        ("rejected", "object_missing"),
        ("failed", "worker_error"),
        ("failed", "attempts_exhausted"),
        ("failed", "worker_unavailable"),
    ],
)
def test_a_submission_that_did_not_succeed_brings_the_collectors_reason(
    collector, tmp_path, final_status, code
) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    rejection = {"code": code, "message": f"The worker reported {code}."}
    collector.finish(submission_id, final_status, rejection=rejection)

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.collector_status) == (
        final_status,
        code,
        final_status,
    )
    assert checked.rejection == rejection
    assert checked.receipt == collector.receipt_document(submission_id)


def test_a_final_status_whose_receipt_is_not_final_keeps_awaiting(
    collector, tmp_path
) -> None:
    """A final status whose receipt answers 409 keeps the submission awaiting.

    An operator's reprocessing can return a final submission to queued between
    the two requests: the receipt then answers 409.
    """
    client, submission_id = accepted_submission(collector, tmp_path)
    collector.finish(submission_id, "succeeded", result={})
    collector.receipt_answers = [
        (
            409,
            {
                "detail": "Submission is not final yet.",
                "code": "submission_not_final",
                "status": "queued",
            },
        )
    ]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.collector_status, checked.receipt) == (
        "awaiting_final_status",
        "queued",
        None,
    )


def test_a_receipt_whose_digest_does_not_match_is_not_kept(collector, tmp_path) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    collector.finish(submission_id, "succeeded", result={})
    collector.corrupt_receipts = True

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.receipt) == (
        "awaiting_final_status",
        "INVALID_RESPONSE",
        None,
    )


def test_a_receipt_for_another_submission_is_not_kept(collector, tmp_path) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    collector.finish(submission_id, "succeeded", result={})
    other = {**collector.receipt_document(submission_id), "submission_id": "sub-other"}
    collector.receipt_answers = [(200, other)]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code) == (
        "awaiting_final_status",
        "INVALID_RESPONSE",
    )


@pytest.mark.parametrize("request_name", ["status", "receipt"])
def test_an_unknown_submission_at_a_status_check_is_created_again(
    collector, tmp_path, request_name
) -> None:
    """On a 404 ``submission_not_found`` answer, the submission is created again.

    The collector can answer 404 ``submission_not_found``, for example, after its
    database was reset.
    """
    client, submission_id = accepted_submission(collector, tmp_path)
    if request_name == "status":
        del collector.submissions[submission_id]
    else:
        collector.finish(submission_id, "succeeded", result={})
        collector.receipt_answers = [(404, "submission_not_found")]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code) == ("pending", "SUBMISSION_NOT_FOUND")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_collector_outage_at_a_status_check_keeps_awaiting(
    collector, tmp_path, status
) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    collector.status_answers = [status]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.collector_status) == (
        "awaiting_final_status",
        "COLLECTOR_UNAVAILABLE",
        None,
    )


def test_a_receipt_outage_keeps_awaiting_with_the_final_status_seen(
    collector, tmp_path
) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    collector.finish(submission_id, "succeeded", result={})
    collector.receipt_answers = [503]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code, checked.collector_status) == (
        "awaiting_final_status",
        "COLLECTOR_UNAVAILABLE",
        "succeeded",
    )


@pytest.mark.parametrize("intake", ["off", "absent"])
def test_a_collector_without_intake_at_a_status_check_waits_an_hour(
    collector, tmp_path, intake
) -> None:
    clock = ManualClock()
    client, submission_id = accepted_submission(collector, tmp_path, clock=clock)
    collector.intake = intake

    first = client.check_status(submission_id)
    requests_after_first = len(collector.status_requests)
    clock.now += 1800
    second = client.check_status(submission_id)

    assert (first.status, first.error_code, first.retry_after) == (
        "awaiting_final_status",
        "COLLECTOR_INTAKE_NOT_ENABLED",
        3600.0,
    )
    assert (second.status, second.error_code) == (
        "awaiting_final_status",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )
    # Nothing is sent to that collector within the hour.
    assert len(collector.status_requests) == requests_after_first


def test_a_403_without_a_code_at_a_status_check_exchanges_again(
    collector, tmp_path
) -> None:
    client, submission_id = accepted_submission(collector, tmp_path)
    exchanges_before = len(collector.exchange_requests)
    collector.status_answers = [403]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.collector_status) == (
        "awaiting_final_status",
        "queued",
    )
    assert len(collector.exchange_requests) == exchanges_before + 1
    assert collector.status_requests == [submission_id, submission_id]


@pytest.mark.parametrize(
    ("request_name", "collector_status"),
    [("status", None), ("receipt", "succeeded")],
)
def test_another_users_token_at_a_status_check_keeps_awaiting(
    collector, tmp_path, request_name, collector_status
) -> None:
    """On a 403 ``submission_owned_by_another_user`` answer, status checks go on.

    The collector has the submission, and only this token's user cannot read it,
    for example when the next emitter on the machine runs with another Hugging
    Face token. The submission did not fail, so checks go on, as for a refused
    token.
    """
    client, submission_id = accepted_submission(collector, tmp_path)
    refusal = (403, "submission_owned_by_another_user")
    if request_name == "status":
        collector.status_answers = [refusal]
    else:
        collector.finish(submission_id, "succeeded", result={})
        collector.receipt_answers = [refusal]

    checked = client.check_status(submission_id)

    assert (
        checked.status,
        checked.error_code,
        checked.collector_status,
        checked.receipt,
    ) == (
        "awaiting_final_status",
        "COLLECTOR_REFUSED_403",
        collector_status,
        None,
    )


def test_an_expired_session_at_a_status_check_is_exchanged_again(
    collector, tmp_path
) -> None:
    invalidations = Invalidations()
    client, submission_id = accepted_submission(
        collector, tmp_path, invalidations=invalidations
    )
    collector.status_answers = [401]

    checked = client.check_status(submission_id)

    assert (checked.status, checked.error_code) == (
        "awaiting_final_status",
        "COLLECTOR_UNAVAILABLE",
    )
    assert invalidations.count == 1


def test_a_status_check_reads_no_file(collector, tmp_path) -> None:
    """Status checks never need the build's files: they may be gone."""
    client, submission_id = accepted_submission(collector, tmp_path)
    for name in ("calibration_diagnostics.json", "build_manifest.json"):
        (tmp_path / name).unlink()
    collector.finish(submission_id, "succeeded", result={})

    assert client.check_status(submission_id).status == "succeeded"

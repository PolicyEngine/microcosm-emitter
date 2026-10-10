"""Exercise the unchanged collector, real PostgreSQL, and its dashboard documents."""

import os
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun
from microcosm_emitter.telemetry.protocol import (
    MAX_TELEMETRY_DETAILS_BYTES,
    MAX_TELEMETRY_MESSAGE_CHARS,
)
from microcosm_emitter.telemetry.service import collector as delivery_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.constants import (
    BATCH_SIZE,
    LOCAL_ONLY_REJECTED_EVENTS,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_EVENTS_REQUEST_BYTES,
    RUN_REGISTRATION_PATH,
    TOKEN_EXCHANGE_PATH,
)
from microcosm_emitter.telemetry.service.database import create_spool_engine
from microcosm_emitter.telemetry.service.models import TelemetryRunRecord
from microcosm_emitter.telemetry.service.sanitization import sanitize_details
from microcosm_emitter.telemetry.service.spool import EventSpool

pytestmark = pytest.mark.collector


@pytest.fixture
def live_collector():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip(
            "Requires disposable TEST_DATABASE_URL and pinned collector installation"
        )
    url = make_url(database_url)
    assert (
        url.host in {"127.0.0.1", "localhost"} and url.database == "telemetry_test"
    ), "Only the disposable loopback test database is allowed"
    import httpx
    import uvicorn
    from telemetry_collector.app import CollectorSettings, create_app
    from telemetry_collector.auth import HuggingFacePrincipal
    from telemetry_collector.migrate import upgrade_database, verify_database
    from telemetry_collector.storage import PostgresTelemetryStore

    class FakeIdentityProvider:
        def authenticate(self, token, required_org):
            assert token in {
                "hf_test_member",
                "hf_test_other_member",
                "hf_test_outsider",
            }
            return HuggingFacePrincipal(
                user_id=(
                    "other-test-user"
                    if token == "hf_test_other_member"
                    else "test-user"
                ),
                username="fixture",
                organizations=() if token == "hf_test_outsider" else (required_org,),
            )

    upgrade_database(database_url)
    verify_database(database_url)
    store = PostgresTelemetryStore(database_url)
    app = create_app(
        settings=CollectorSettings(
            jwt_secret="synthetic-jwt-test-secret-at-least-32-characters",
            read_token="synthetic-dashboard-read-token",
        ),
        store=store,
        huggingface_authenticator=FakeIdentityProvider(),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=address,
                headers={"X-Telemetry-Read-Token": "synthetic-dashboard-read-token"},
            ) as reader:
                yield address, reader
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def test_service_events_materialize_as_dashboard_documents(
    tmp_path, monkeypatch, live_collector
):
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run_id = uuid.uuid4().hex
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun(run_id, "UK", "contract"),
        spool_path=tmp_path / "events.sqlite3",
        development_collector_url=address,
    )
    assert emitter.available
    try:
        emitter.transition_stage("hf_download")
        emitter.progress("hf_download", done=1, total=1, unit="files")
        emitter.transition_stage("compile_targets")
        emitter.progress("compile_targets", done=2, total=4, unit="batches")
        emitter.transition_calibration_progress(
            {"kind": "calibration_epoch", "epoch": 1, "loss": 0.5}
        )
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    response = reader.get(f"/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["progress"]["status"] == "completed"
    download_events = [
        event for event in document["events"] if event["stage_id"] == "hf_download"
    ]
    assert [(event["event_type"], event["status"]) for event in download_events] == [
        ("stage", "started"),
        ("stage", "progress"),
        ("stage", "completed"),
    ]
    assert download_events[1]["details"] == {"done": 1, "total": 1, "unit": "files"}
    assert any(event["event_type"] == "calibration" for event in document["events"])
    assert document["events"][-1]["resources"]["rss_bytes"] > 0
    assert len({event["event_id"] for event in document["events"]}) == len(
        document["events"]
    )
    assert reader.get("/v1/runs", params={"country": "UK"}).status_code == 200


def test_rejected_identity_never_registers_or_uploads_run(
    tmp_path, monkeypatch, live_collector
):
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_outsider")
    run_id = uuid.uuid4().hex
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun(run_id, "UK", "contract"),
        spool_path=tmp_path / "events.sqlite3",
        development_collector_url=address,
    )
    assert emitter.available
    try:
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    assert reader.get(f"/v1/runs/{run_id}").status_code == 404
    spool = EventSpool(tmp_path / "events.sqlite3")
    try:
        assert spool.has_pending() and not spool.has_deliverable()
    finally:
        spool.close()


def test_lost_ack_retries_same_event_after_restart_without_duplicates(
    tmp_path, monkeypatch, live_collector
):
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun(uuid.uuid4().hex, "UK", "contract")
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(run.as_registration())
    event = spool.append(
        run.as_registration(), {"event_type": "run", "status": "started"}
    )
    real_post = delivery_module._http_post

    def lose_ack(url, *args, **kwargs):
        response = real_post(url, *args, **kwargs)
        if url.endswith("/events"):
            assert response[0] == 202
            raise OSError("simulated lost acknowledgement")
        return response

    monkeypatch.setattr(delivery_module, "_http_post", lose_ack)
    first = CollectorDelivery(
        spool, run.as_registration(), development_collector_url=address
    )
    assert not first.flush_once()
    assert spool.has_pending()
    # The service exits, releasing its lease, and a new one starts.
    first.close()
    spool.close()
    monkeypatch.setattr(delivery_module, "_http_post", real_post)
    reopened = EventSpool(path)
    restarted = CollectorDelivery(
        reopened, run.as_registration(), development_collector_url=address
    )
    try:
        assert restarted.flush_once()
        assert not reopened.has_pending()
    finally:
        restarted.close()
        reopened.close()
    response = reader.get(f"/v1/runs/{run.run_id}")
    assert response.status_code == 200
    assert [entry["event_id"] for entry in response.json()["events"]] == [
        event["event_id"]
    ]


def _local_only_reason(path, run: TelemetryRun) -> str | None:
    engine = create_spool_engine(path)
    try:
        with Session(engine) as session:
            stored = session.get(TelemetryRunRecord, (run.run_id, run.producer_id))
            return stored.local_only_reason
    finally:
        engine.dispose()


def _recording_post(monkeypatch) -> list[tuple[str, int, int]]:
    """Record every request the delivery sends: path, body bytes, status."""

    real_post = delivery_module._http_post
    sent: list[tuple[str, int, int]] = []

    def recording_post(url, payload, *args, **kwargs):
        response = real_post(url, payload, *args, **kwargs)
        size = len(delivery_module._request_body(payload))
        sent.append((urlsplit(url).path, size, response[0]))
        return response

    monkeypatch.setattr(delivery_module, "_http_post", recording_post)
    return sent


def _collector_token(address: str, hf_token: str) -> str:
    status, body = delivery_module._http_post(
        address + TOKEN_EXCHANGE_PATH, {}, hf_token
    )
    assert status == 200, body
    return body["access_token"]


def test_an_event_outside_the_schema_makes_the_run_local_only_instead_of_retrying(
    tmp_path, monkeypatch, capsys, live_collector
):
    """The collector's own answer to a batch it will never accept: 422."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun(uuid.uuid4().hex, "UK", "contract")
    registration = run.as_registration()
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration)
    spool.append(
        registration, {"event_type": "not_a_collector_event_type", "status": "started"}
    )
    sent = _recording_post(monkeypatch)
    delivery = CollectorDelivery(spool, registration, development_collector_url=address)
    events_path = f"/v1/runs/{run.run_id}/events"
    try:
        assert not delivery.flush_once()
        assert not delivery.flush_once()
        assert [code for where, _, code in sent if where == events_path] == [422]
        assert spool.has_pending() and not spool.has_deliverable()
    finally:
        delivery.close()
        spool.close()
    assert _local_only_reason(path, run) == LOCAL_ONLY_REJECTED_EVENTS
    warnings = capsys.readouterr().err
    assert warnings.count("local-only for this run") == 1
    assert "HTTP 422" in warnings
    # The collector kept none of the refused batch.
    document = reader.get(f"/v1/runs/{run.run_id}")
    assert document.status_code == 200, document.text
    assert document.json()["events"] == []


def test_a_run_the_collector_does_not_have_is_registered_again_and_delivered(
    tmp_path, monkeypatch, capsys, live_collector
):
    """The collector answers 404 for the events of a run it does not have. The
    service then registers the run again, which the collector accepts, and the
    same batch arrives."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun(uuid.uuid4().hex, "UK", "contract")
    registration = run.as_registration()
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration)
    event = spool.append(registration, {"event_type": "run", "status": "started"})
    real_post = delivery_module._http_post
    sent: list[tuple[str, int]] = []

    def first_registration_is_lost(url, payload, *args, **kwargs):
        where = urlsplit(url).path
        if where == RUN_REGISTRATION_PATH and not any(
            sent_path == RUN_REGISTRATION_PATH for sent_path, _ in sent
        ):
            # Answered as registered, but the collector never keeps it: as
            # one that lost the run afterwards, its database restored, say.
            sent.append((where, 201))
            return 201, {"registered": True}
        response = real_post(url, payload, *args, **kwargs)
        sent.append((where, response[0]))
        return response

    monkeypatch.setattr(delivery_module, "_http_post", first_registration_is_lost)
    delivery = CollectorDelivery(spool, registration, development_collector_url=address)
    try:
        assert not delivery.flush_once()
        assert delivery.flush_once()
        assert not spool.has_pending()
    finally:
        delivery.close()
        spool.close()
    events_path = f"/v1/runs/{run.run_id}/events"
    assert sent == [
        (TOKEN_EXCHANGE_PATH, 200),
        (RUN_REGISTRATION_PATH, 201),
        (events_path, 404),
        (RUN_REGISTRATION_PATH, 201),
        (events_path, 202),
    ]
    assert _local_only_reason(path, run) is None
    assert capsys.readouterr().err == ""
    document = reader.get(f"/v1/runs/{run.run_id}")
    assert document.status_code == 200, document.text
    assert [entry["event_id"] for entry in document.json()["events"]] == [
        event["event_id"]
    ]


@pytest.mark.parametrize(
    ("refusal", "status", "says"),
    [
        ("owner_with_other_metadata", 409, "rejected its registration (HTTP 409)"),
        ("outside_schema", 422, "rejected its registration (HTTP 422)"),
        ("another_users_run", 403, "refused the ambient Hugging Face user"),
    ],
)
def test_a_refused_registration_warns_about_the_registration_not_the_credential(
    tmp_path, monkeypatch, capsys, live_collector, refusal, status, says
):
    """The collector's own answers to a registration: 409 to the run's owner
    for other metadata, 422 outside its schema, 403 for another user's run. In
    each the token exchange had accepted the login as an organization member."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run_id = uuid.uuid4().hex
    # A lower-case country code is outside the collector's schema.
    run = TelemetryRun(
        run_id, "uk" if refusal == "outside_schema" else "UK", "contract"
    )
    registration = run.as_registration()
    if refusal != "outside_schema":
        # Someone registered the run id first: this user with another
        # pipeline, or another member with the same registration.
        another_user = refusal == "another_users_run"
        seeded = {
            **registration,
            "producer_id": uuid.uuid4().hex,
            "pipeline": "contract" if another_user else "another-pipeline",
        }
        token = _collector_token(
            address, "hf_test_other_member" if another_user else "hf_test_member"
        )
        answer = delivery_module._http_post(
            address + RUN_REGISTRATION_PATH, seeded, token
        )
        assert answer[0] == 201, answer
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration)
    spool.append(registration, {"event_type": "run", "status": "started"})
    sent = _recording_post(monkeypatch)
    delivery = CollectorDelivery(spool, registration, development_collector_url=address)
    try:
        assert not delivery.flush_once()
        assert not delivery.flush_once()
        assert [(where, code) for where, _, code in sent] == [
            (TOKEN_EXCHANGE_PATH, 200),
            (RUN_REGISTRATION_PATH, status),
        ]
        assert spool.has_pending() and not spool.has_deliverable()
    finally:
        delivery.close()
        spool.close()
    assert _local_only_reason(path, run) == LOCAL_ONLY_REJECTED_REGISTRATION
    warnings = capsys.readouterr().err
    assert warnings.count("local-only for this run") == 1
    assert says in warnings and f"HTTP {status}" in warnings
    assert "organization member" not in warnings
    # None of the run's events reached the collector.
    document = reader.get(f"/v1/runs/{run_id}")
    if refusal == "outside_schema":
        assert document.status_code == 404
    else:
        assert document.status_code == 200, document.text
        assert document.json()["events"] == []


def _largest_event() -> dict:
    """The largest event the service queues: a message of the most characters,
    each one json.dumps escapes to twelve bytes, and details at their limit."""

    details = {}
    while True:
        grown = {**details, f"k{len(details)}": "x" * 190}
        if len(delivery_module._request_body(grown)) > MAX_TELEMETRY_DETAILS_BYTES - 16:
            break
        details = grown
    padding = (
        MAX_TELEMETRY_DETAILS_BYTES
        - len(delivery_module._request_body(details))
        - len(',"pad":""')
    )
    details["pad"] = "x" * padding
    assert len(delivery_module._request_body(details)) == MAX_TELEMETRY_DETAILS_BYTES
    assert sanitize_details(details) == details
    return {
        "event_type": "stage",
        "stage_id": "s" * 192,
        "status": "progress",
        "message": "\U0001f600" * MAX_TELEMETRY_MESSAGE_CHARS,
        "details": details,
    }


def test_a_full_batch_of_the_largest_events_arrives_in_requests_the_collector_takes(
    tmp_path, monkeypatch, capsys, live_collector
):
    """A hundred of the largest events are over the collector's 1 MiB body
    limit together. They are sent in two requests, and all of them arrive."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun("r" + uuid.uuid4().hex + "r" * 159, "UK", "contract")
    assert len(run.run_id) == 192
    registration = run.as_registration()
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration)
    queued = spool.append_many(registration, [_largest_event()] * BATCH_SIZE)
    whole = len(delivery_module._request_body({"events": queued}))
    assert whole > MAX_EVENTS_REQUEST_BYTES, whole
    sent = _recording_post(monkeypatch)
    delivery = CollectorDelivery(spool, registration, development_collector_url=address)
    events_path = f"/v1/runs/{run.run_id}/events"
    try:
        assert delivery.flush_once()
        assert delivery.flush_once()
        assert not spool.has_pending()
    finally:
        delivery.close()
        spool.close()
    requests = [(size, code) for where, size, code in sent if where == events_path]
    assert [code for _, code in requests] == [202, 202]
    assert all(size <= MAX_EVENTS_REQUEST_BYTES for size, _ in requests), requests
    assert _local_only_reason(path, run) is None
    assert capsys.readouterr().err == ""
    document = reader.get(f"/v1/runs/{run.run_id}")
    assert document.status_code == 200, document.text
    assert [entry["event_id"] for entry in document.json()["events"]] == [
        event["event_id"] for event in queued
    ]


def test_the_collector_takes_a_request_body_of_exactly_the_limit(
    tmp_path, monkeypatch, live_collector
):
    """The byte budget is the collector's limit itself, with no margin: a
    request of exactly that many bytes is accepted."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun(uuid.uuid4().hex, "UK", "contract")
    registration = run.as_registration()
    spool = EventSpool(tmp_path / "events.sqlite3")
    try:
        spool.register(registration)
        event = spool.append(registration, {"event_type": "run", "status": "started"})
    finally:
        spool.close()
    token = _collector_token(address, "hf_test_member")
    registered = delivery_module._http_post(
        address + RUN_REGISTRATION_PATH, registration, token
    )
    assert registered[0] == 201, registered
    # Pad the one event until its request is the limit to the byte.
    spare = MAX_EVENTS_REQUEST_BYTES - len(
        delivery_module._request_body({"events": [{**event, "details": {"pad": ""}}]})
    )
    payload = {"events": [{**event, "details": {"pad": "x" * spare}}]}
    assert len(delivery_module._request_body(payload)) == MAX_EVENTS_REQUEST_BYTES

    status, body = delivery_module._http_post(
        f"{address}/v1/runs/{run.run_id}/events", payload, token
    )

    assert (status, body) == (202, {"accepted": 1, "duplicates": 0})
    document = reader.get(f"/v1/runs/{run.run_id}")
    assert document.status_code == 200, document.text
    assert [entry["event_id"] for entry in document.json()["events"]] == [
        event["event_id"]
    ]

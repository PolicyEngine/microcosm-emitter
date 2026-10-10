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
from microcosm_emitter.telemetry.service import collector as delivery_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.constants import LOCAL_ONLY_REJECTED_EVENTS
from microcosm_emitter.telemetry.service.database import create_spool_engine
from microcosm_emitter.telemetry.service.models import TelemetryRunRecord
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
            assert token in {"hf_test_member", "hf_test_outsider"}
            return HuggingFacePrincipal(
                user_id="test-user",
                username="fixture",
                organizations=(required_org,) if token == "hf_test_member" else (),
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


@pytest.mark.parametrize(
    ("refusal", "status"),
    [("event_outside_schema", 422), ("run_not_registered", 404)],
)
def test_a_settled_refusal_makes_the_run_local_only_instead_of_retrying(
    tmp_path, monkeypatch, capsys, live_collector, refusal, status
):
    """The collector's own answers to a batch it will never accept: 422 for an
    event outside its schema, 404 for a run it does not know."""
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run = TelemetryRun(uuid.uuid4().hex, "UK", "contract")
    registration = run.as_registration()
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration)
    event_type = "not_a_collector_event_type" if status == 422 else "run"
    spool.append(registration, {"event_type": event_type, "status": "started"})
    real_post = delivery_module._http_post
    answers: list[tuple[str, int]] = []

    def recording_post(url, *args, **kwargs):
        response = real_post(url, *args, **kwargs)
        answers.append((urlsplit(url).path, response[0]))
        return response

    monkeypatch.setattr(delivery_module, "_http_post", recording_post)
    delivery = CollectorDelivery(spool, registration, development_collector_url=address)
    if refusal == "run_not_registered":
        # As if the collector no longer had a run this service registered.
        delivery._registered.add((run.run_id, run.producer_id))
    events_path = f"/v1/runs/{run.run_id}/events"
    try:
        assert not delivery.flush_once()
        assert not delivery.flush_once()
        assert [code for sent, code in answers if sent == events_path] == [status]
        assert spool.has_pending() and not spool.has_deliverable()
    finally:
        delivery.close()
        spool.close()
    engine = create_spool_engine(path)
    try:
        with Session(engine) as session:
            stored = session.get(TelemetryRunRecord, (run.run_id, run.producer_id))
            assert stored.local_only_reason == LOCAL_ONLY_REJECTED_EVENTS
    finally:
        engine.dispose()
    warnings = capsys.readouterr().err
    assert warnings.count("local-only for this run") == 1
    assert f"HTTP {status}" in warnings
    # The collector kept none of the refused batch.
    document = reader.get(f"/v1/runs/{run.run_id}")
    if refusal == "run_not_registered":
        assert document.status_code == 404
    else:
        assert document.status_code == 200, document.text
        assert document.json()["events"] == []

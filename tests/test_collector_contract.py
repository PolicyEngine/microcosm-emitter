"""Exercise the unchanged collector, real PostgreSQL, and its dashboard documents."""

import os
import socket
import threading
import time
import uuid

import pytest
from sqlalchemy.engine import make_url

from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun
from microcosm_emitter.telemetry.service import collector as delivery_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
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
            error_message = "simulated lost acknowledgement"
            raise OSError(error_message)
        return response

    monkeypatch.setattr(delivery_module, "_http_post", lose_ack)
    assert not CollectorDelivery(spool, development_collector_url=address).flush_once()
    assert spool.has_pending()
    spool.close()
    monkeypatch.setattr(delivery_module, "_http_post", real_post)
    reopened = EventSpool(path)
    try:
        assert CollectorDelivery(
            reopened, development_collector_url=address
        ).flush_once()
        assert not reopened.has_pending()
    finally:
        reopened.close()
    response = reader.get(f"/v1/runs/{run.run_id}")
    assert response.status_code == 200
    assert [entry["event_id"] for entry in response.json()["events"]] == [
        event["event_id"]
    ]


@pytest.mark.parametrize("unnamed", [False, True])
def test_blocked_run_projects_check_metadata_without_failure(
    tmp_path,
    monkeypatch,
    live_collector,
    unnamed,
):
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run_id = uuid.uuid4().hex
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun(run_id, "UK", "contract"),
        spool_path=tmp_path / "blocked.sqlite3",
        development_collector_url=address,
    )
    assert emitter.available
    check_ids = [] if unnamed else ["uk_local_target_fit"]
    count = 3 if unnamed else 1
    try:
        emitter.transition_stage("hf_download")
        emitter.block(
            phase="preflight",
            blocking_gate_ids=check_ids,
            blocking_failure_count=count,
            gate_statuses={
                "uk_local_target_fit": "failed",
                "HF_TOKEN": "hf_private_check",
            },
        )
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    response = reader.get(f"/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["progress"]["status"] == "blocked"
    assert document["progress"]["current_stage"] == "blocked"
    assert document["progress"]["failure"] is None
    expected = {
        "phase": "preflight",
        "blocking_gate_ids": check_ids,
        "blocking_failure_count": count,
    }
    assert document["progress"]["block"] == expected
    assert document["run_manifest"]["block"] == expected
    assert document["run_manifest"]["failure"] is None
    assert [(event["stage_id"], event["status"]) for event in document["events"]] == [
        ("created", "started"),
        ("hf_download", "started"),
        ("hf_download", "completed"),
        ("blocked", "blocked"),
    ]
    assert document["events"][-1]["details"] == {
        **expected,
        "gate_statuses": {"uk_local_target_fit": "failed", "HF_TOKEN": "[redacted]"},
    }
    assert "hf_private_check" not in response.text


@pytest.mark.parametrize(
    "failure_class,error_code,error",
    [
        ("error", "BUILD_FAILED", RuntimeError("Bearer hf_private_error")),
        (
            "interrupted",
            "BUILD_INTERRUPTED",
            KeyboardInterrupt("Bearer hf_private_error"),
        ),
        ("terminated", "BUILD_TERMINATED", RuntimeError("Bearer hf_private_error")),
        ("oom", "BUILD_OUT_OF_MEMORY", MemoryError("Bearer hf_private_error")),
    ],
)
def test_supplied_failure_codes_survive_collector_storage_and_documents(
    tmp_path,
    monkeypatch,
    live_collector,
    failure_class,
    error_code,
    error,
):
    address, reader = live_collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_member")
    run_id = uuid.uuid4().hex
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun(run_id, "UK", "contract"),
        spool_path=tmp_path / "failed.sqlite3",
        development_collector_url=address,
    )
    assert emitter.available
    try:
        emitter.transition_stage("hf_download")
        emitter.fail(
            error,
            failed_during="hf_download",
            failure_class=failure_class,
            error_code=error_code,
        )
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    response = reader.get(f"/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    document = response.json()
    # Generic details retain credential redaction; the wire stage identity does not.
    expected = {
        "error_code": error_code,
        "error_type": type(error).__name__,
        "failure_class": failure_class,
        "failed_during": "[redacted]",
    }
    assert document["progress"]["status"] == "failed"
    assert document["progress"]["current_stage"] == "failed"
    assert document["progress"]["block"] is None
    assert document["run_manifest"]["failure"] == document["progress"]["failure"]
    assert document["progress"]["failure"] == {**expected, "message": "[redacted]"}
    assert [
        (event["stage_id"], event["status"]) for event in document["events"][-2:]
    ] == [
        ("hf_download", "failed"),
        ("failed", "failed"),
    ]
    assert all(event["details"] == expected for event in document["events"][-2:])
    assert "hf_private_error" not in response.text

"""Installed entry-point discovery, real sockets, and simultaneous build processes."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from policyengine_telemetry.client import LocalTelemetryEmitter, TelemetryRun
from policyengine_telemetry.service.spool import EventSpool


@pytest.fixture
def collector():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], payload))
            if self.path.endswith("/exchange"):
                status, response = (
                    200,
                    {"access_token": "collector-test-token", "expires_in": 3600},
                )
            elif self.path == "/v1/runs":
                status, response = 201, {"registered": True}
            else:
                status, response = (
                    202,
                    {"accepted": len(payload["events"]), "duplicates": 0},
                )
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_real_service_delivers_without_microcosm(tmp_path, monkeypatch, collector):
    address, requests = collector
    monkeypatch.setenv("HF_TOKEN", "hf_test_only")
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("subprocess", "UK", "test"),
        spool_path=tmp_path / "events.sqlite3",
        development_collector_url=address,
        identity={"test": True},
    )
    assert emitter.available
    try:
        emitter.transition_stage("compile")
        emitter.progress("compile", done=2, total=3)
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    assert requests[0] == ("/v1/auth/huggingface/exchange", "Bearer hf_test_only", {})
    events = [
        event
        for path, token, payload in requests
        if path.endswith("/events")
        for event in payload["events"]
    ]
    assert events[0]["details"]["identity"] == {"test": True}
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["status"] == "completed"
    assert events[-1]["resources"]["rss_bytes"] > 0
    assert not emitter._handle.runtime_dir.exists()


def test_simultaneous_builds_share_queue_without_auth_or_uploads(tmp_path, collector):
    address, requests = collector
    path = tmp_path / "shared.sqlite3"

    def build(number):
        emitter = LocalTelemetryEmitter.start(
            run=TelemetryRun("concurrent", "UK", "test", producer_id=str(number)),
            spool_path=path,
            development_collector_url=address,
        )
        assert emitter.available
        try:
            emitter.stage("compile")
            emitter.complete()
            assert emitter._handle.process.wait(timeout=10) == 0
        finally:
            emitter.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(build, range(4)))
    assert requests == []
    spool = EventSpool(path)
    try:
        assert not spool.pending_runs()
        for number in range(4):
            assert [
                event["sequence"] for event in spool.batch("concurrent", str(number))
            ] == [1, 2, 3]
    finally:
        spool.close()

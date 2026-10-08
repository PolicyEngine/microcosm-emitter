import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from alembic import command
from policyengine_telemetry.client import (
    TelemetryRun,
)
from policyengine_telemetry.protocol import (
    MAX_TELEMETRY_DETAILS_BYTES,
)
from policyengine_telemetry.sanitization import (
    sanitize_details,
    sanitize_json,
    sanitize_text,
)
from policyengine_telemetry.service import collector as collector_module
from policyengine_telemetry.service import resources as resources_module
from policyengine_telemetry.service import spool as spool_module
from policyengine_telemetry.service.collector import CollectorDelivery
from policyengine_telemetry.service.constants import (
    PRODUCTION_COLLECTOR_URL,
)
from policyengine_telemetry.service.database import (
    create_spool_engine,
)
from policyengine_telemetry.service.migrations import (
    alembic_config,
    current_database_revision,
    migration_head_revision,
)
from policyengine_telemetry.service.models import (
    serialized_json_length,
)
from policyengine_telemetry.service.spool import EventSpool


def _registration(run_id: str = "run-a") -> dict[str, object]:
    return TelemetryRun(
        run_id=run_id,
        country_code="US",
        pipeline="us_fiscal_refresh",
        candidate_id="candidate-a",
        producer_id="producer-a",
    ).as_registration()


def _event(stage_id: str = "compile_targets") -> dict[str, object]:
    return {
        "timestamp": "2026-10-02T10:00:00+00:00",
        "event_type": "stage",
        "stage_id": stage_id,
        "status": "started",
        "message": "Compiling targets.",
        "details": {"batches": 12},
    }


def test_development_collector_must_be_on_loopback() -> None:
    assert collector_module._development_collector_url("http://127.0.0.1:8080") == (
        "http://127.0.0.1:8080"
    )
    for value in ("https://collector.example", "http://192.0.2.1:8080"):
        try:
            collector_module._development_collector_url(value)
        except ValueError as error:
            assert "loopback" in str(error)
        else:
            raise AssertionError("non-loopback development collector was accepted")


def test_production_collector_cannot_be_replaced_by_environment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "MICROCOSM_TELEMETRY_COLLECTOR_URL",
        "https://untrusted.example",
    )

    delivery = CollectorDelivery(EventSpool(tmp_path / "events.sqlite3"))

    assert delivery.collector_url == PRODUCTION_COLLECTOR_URL


def test_token_bearing_http_post_does_not_follow_redirects() -> None:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/credential-leak")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        status, _ = collector_module._http_post(
            f"http://127.0.0.1:{server.server_port}/exchange",
            {"run_id": "run-a"},
            "hf-private-token",
        )
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()

    assert status == 307
    assert paths == ["/exchange"]


def test_outbound_payload_redacts_credentials_and_tracebacks() -> None:
    assert sanitize_text("Bearer hf_abcdefghijk") == "[redacted]"
    assert sanitize_json(
        {
            "HF_TOKEN": "hf_abcdefghijk",
            "traceback": "private stack",
            "message": "credential=top-secret",
        }
    ) == {
        "HF_TOKEN": "[redacted]",
        "traceback": "[redacted]",
        "message": "[redacted]",
    }
    bounded = sanitize_details(
        {"done": 2, **{f"large_{index}": "x" * 2_000 for index in range(10)}}
    )
    assert bounded["done"] == 2
    assert bounded["telemetry_details_truncated"] is True
    assert len(json.dumps(bounded).encode()) <= MAX_TELEMETRY_DETAILS_BYTES


def test_event_spool_assigns_stable_sequences_and_acknowledges(tmp_path) -> None:
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)

    first = spool.append(registration, _event())
    second = spool.append(registration, _event("calibrate"))

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["producer_id"] == "producer-a"
    assert [event["sequence"] for event in spool.batch("run-a", "producer-a")] == [
        1,
        2,
    ]

    spool.acknowledge([first["event_id"]])
    assert [event["sequence"] for event in spool.batch("run-a", "producer-a")] == [2]
    assert current_database_revision(spool_path) == migration_head_revision()


def test_alembic_schema_matches_sqlalchemy_models(tmp_path) -> None:
    spool_path = tmp_path / "events.sqlite3"
    EventSpool(spool_path)
    engine = create_spool_engine(spool_path)
    try:
        with engine.begin() as connection:
            with alembic_config(connection=connection) as config:
                command.check(config)
    finally:
        engine.dispose()


def test_event_spool_keeps_repeated_run_producers_separate(tmp_path) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    first = _registration()
    second = {**first, "producer_id": "producer-b"}
    spool.register(first)
    spool.register(second)

    spool.append(first, _event())
    spool.append(second, _event())

    assert spool.batch("run-a", "producer-a")[0]["sequence"] == 1
    assert spool.batch("run-a", "producer-b")[0]["sequence"] == 1
    assert len(spool.pending_runs()) == 2


def test_event_spool_prunes_oldest_events_to_size_limit(
    tmp_path,
    monkeypatch,
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    first = spool.append(registration, _event("first"))
    second = spool.append(registration, _event("second"))
    monkeypatch.setattr(
        spool_module,
        "MAX_QUEUED_BYTES",
        serialized_json_length(second),
    )

    spool.prune()

    assert first["event_id"] != second["event_id"]
    assert spool.batch("run-a", "producer-a") == [second]


def test_collector_delivery_exchanges_hf_token_then_flushes(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    queued = spool.append(registration, _event())
    requests: list[tuple[str, dict[str, object], str]] = []

    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-secret")

    def fake_post(url, payload, token, *, timeout=5.0):
        requests.append((url, payload, token))
        if url.endswith("/v1/auth/huggingface/exchange"):
            return 200, {"access_token": "collector-token", "expires_in": 3600}
        if url.endswith("/v1/runs"):
            return 201, {"registered": True}
        return 202, {"accepted": 1, "duplicates": 0}

    monkeypatch.setattr(collector_module, "_http_post", fake_post)

    delivery = CollectorDelivery(
        spool,
        development_collector_url="http://127.0.0.1:8080",
    )
    assert delivery.flush_once()
    assert requests[0][2] == "hf-secret"
    assert requests[0][1] == {}
    assert requests[1] == (
        "http://127.0.0.1:8080/v1/runs",
        registration,
        "collector-token",
    )
    assert requests[2][2] == "collector-token"
    assert requests[2][1] == {"events": [queued]}
    assert not spool.has_pending()


def test_non_org_credential_keeps_event_local(tmp_path, monkeypatch, capsys) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-outsider")

    def reject(url, payload, token, *, timeout=5.0):
        requests.append(payload)
        return 403, {"detail": "not a member"}

    monkeypatch.setattr(collector_module, "_http_post", reject)
    delivery = CollectorDelivery(
        spool,
        development_collector_url="http://127.0.0.1:8080",
    )

    assert not delivery.flush_once()
    assert not delivery.flush_once()
    assert spool.has_pending()
    assert spool.pending_runs() == []
    assert requests == [{}]
    assert capsys.readouterr().err.count("local-only for this run") == 1


def test_identity_provider_outage_keeps_events_eligible_for_retry(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-member")
    monkeypatch.setattr(
        collector_module,
        "_http_post",
        lambda *args, **kwargs: (503, {"detail": "temporarily unavailable"}),
    )

    assert not CollectorDelivery(spool).flush_once()
    assert spool.pending_runs() == [registration]


def test_missing_credential_never_contacts_collector_and_stays_local_only(
    tmp_path, monkeypatch
) -> None:
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: None)
    monkeypatch.setattr(
        collector_module,
        "_http_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network request")
        ),
    )

    delivery = CollectorDelivery(spool)
    assert not delivery.flush_once()
    assert spool.pending_runs() == []

    reopened = EventSpool(spool_path)
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-later")
    assert reopened.pending_runs() == []


def test_resource_sampler_keeps_reaped_child_cpu_monotonic(monkeypatch) -> None:
    state = {"child_alive": True}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return float(self.pid)

        def children(self, recursive=False):
            assert recursive is True
            if self.pid == 10 and state["child_alive"]:
                return [FakeProcess(11)]
            return []

        def cpu_times(self):
            if self.pid == 10:
                return SimpleNamespace(
                    user=10.0,
                    system=2.0,
                    children_user=100.0 if not state["child_alive"] else 0.0,
                    children_system=20.0 if not state["child_alive"] else 0.0,
                )
            return SimpleNamespace(
                user=80.0,
                system=15.0,
                children_user=0.0,
                children_system=0.0,
            )

        def memory_info(self):
            return SimpleNamespace(rss=100)

    monkeypatch.setattr(
        resources_module,
        "psutil",
        SimpleNamespace(Process=FakeProcess, Error=OSError, STATUS_ZOMBIE="zombie"),
    )
    sampler = resources_module.ProcessTreeSampler(10)

    before = sampler.sample()
    state["child_alive"] = False
    after = sampler.sample()

    assert before["cpu_user_seconds"] == 90.0
    assert before["cpu_system_seconds"] == 17.0
    assert after["cpu_user_seconds"] == 110.0
    assert after["cpu_system_seconds"] == 22.0

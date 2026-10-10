import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from alembic import command
from sqlalchemy.orm import Session

from microcosm_emitter.telemetry.client import (
    TelemetryRun,
)
from microcosm_emitter.telemetry.protocol import (
    MAX_TELEMETRY_DETAILS_BYTES,
)
from microcosm_emitter.telemetry.service import collector as collector_module
from microcosm_emitter.telemetry.service import resources as resources_module
from microcosm_emitter.telemetry.service import spool as spool_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.constants import (
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_EVENTS,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_RETRY_SECONDS,
    PRODUCTION_COLLECTOR_URL,
    RUN_REGISTRATION_PATH,
    TOKEN_EXCHANGE_PATH,
)
from microcosm_emitter.telemetry.service.database import (
    create_spool_engine,
)
from microcosm_emitter.telemetry.service.migrations import (
    alembic_config,
    current_database_revision,
    migration_head_revision,
)
from microcosm_emitter.telemetry.service.models import (
    TelemetryRunRecord,
    serialized_json_length,
)
from microcosm_emitter.telemetry.service.sanitization import (
    sanitize_details,
    sanitize_json,
    sanitize_text,
)
from microcosm_emitter.telemetry.service.spool import EventSpool


def _registration(run_id: str = "run-a") -> dict[str, object]:
    return TelemetryRun(
        run_id=run_id,
        country_code="US",
        pipeline="us_fiscal_refresh",
        candidate_id="candidate-a",
        producer_id="producer-a",
    ).as_registration()


@pytest.fixture
def make_delivery():
    """Build deliveries whose leases are released when the test ends."""

    deliveries: list[CollectorDelivery] = []

    def make(*args, **kwargs) -> CollectorDelivery:
        deliveries.append(CollectorDelivery(*args, **kwargs))
        return deliveries[-1]

    yield make
    for delivery in deliveries:
        delivery.close()


def _local_only_reason(spool_path, registration) -> str | None:
    engine = create_spool_engine(spool_path)
    try:
        with Session(engine) as session:
            run = session.get(
                TelemetryRunRecord,
                (registration["run_id"], registration["producer_id"]),
            )
            return run.local_only_reason
    finally:
        engine.dispose()


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
    tmp_path, monkeypatch, make_delivery
) -> None:
    monkeypatch.setenv(
        "MICROCOSM_TELEMETRY_COLLECTOR_URL",
        "https://untrusted.example",
    )

    delivery = make_delivery(EventSpool(tmp_path / "events.sqlite3"), _registration())

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
    tmp_path, monkeypatch, make_delivery
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

    delivery = make_delivery(
        spool,
        registration,
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


def test_non_org_credential_keeps_event_local(
    tmp_path, monkeypatch, capsys, make_delivery
) -> None:
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
    delivery = make_delivery(
        spool,
        registration,
        development_collector_url="http://127.0.0.1:8080",
    )

    assert not delivery.flush_once()
    assert not delivery.flush_once()
    assert spool.has_pending()
    assert spool.pending_runs() == []
    assert requests == [{}]
    assert capsys.readouterr().err.count("local-only for this run") == 1


@pytest.mark.parametrize(
    ("status", "retried"),
    [
        (422, False),
        (404, False),
        (413, False),
        (400, False),
        (429, True),
        (408, True),
        (503, True),
    ],
)
def test_a_settled_collector_rejection_goes_local_only_instead_of_retrying(
    tmp_path, monkeypatch, capsys, make_delivery, status, retried
) -> None:
    """A 4xx the collector will repeat (an event shape it does not accept, say)
    must not wedge the queue: the run goes local-only with a reason and one
    warning. A timeout, a rate limit or a server error still retries."""
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-member")
    paths: list[str] = []
    clock = [0.0]
    monkeypatch.setattr(
        collector_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def fake_post(url, payload, token, *, timeout=5.0):
        paths.append(urlsplit(url).path)
        if url.endswith(TOKEN_EXCHANGE_PATH):
            return 200, {"access_token": "collector-token", "expires_in": 3600}
        if url.endswith(RUN_REGISTRATION_PATH):
            return 201, {"registered": True}
        return status, {"detail": "rejected"}

    monkeypatch.setattr(collector_module, "_http_post", fake_post)
    delivery = make_delivery(
        spool, registration, development_collector_url="http://127.0.0.1:8080"
    )
    events_path = "/v1/runs/run-a/events"
    assert not delivery.flush_once()
    assert spool.has_pending()
    assert paths.count(events_path) == 1
    if retried:
        assert spool.pending_runs() == [registration]
        assert _local_only_reason(spool_path, registration) is None
        assert "local-only" not in capsys.readouterr().err
        # Once the retry delay has passed, the same batch is offered again.
        clock[0] += MAX_RETRY_SECONDS + 1
        assert not delivery.flush_once()
        assert paths.count(events_path) == 2
        assert spool.pending_runs() == [registration]
        return
    assert spool.pending_runs() == []
    assert _local_only_reason(spool_path, registration) == LOCAL_ONLY_REJECTED_EVENTS
    clock[0] += MAX_RETRY_SECONDS + 1
    assert not delivery.flush_once()
    assert paths.count(events_path) == 1
    err = capsys.readouterr().err
    assert err.count("local-only for this run") == 1
    assert f"HTTP {status}" in err


@pytest.mark.parametrize(
    ("status", "retried"),
    [
        (409, False),
        (422, False),
        (404, False),
        (413, False),
        (400, False),
        (429, True),
        (408, True),
        (503, True),
    ],
)
def test_a_settled_registration_rejection_goes_local_only_instead_of_retrying(
    tmp_path, monkeypatch, capsys, make_delivery, status, retried
) -> None:
    """The same rule covers a run's registration: a settled 4xx makes the run
    local-only before any of its events are sent; a timeout, a rate limit or a
    server error is retried."""
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-member")
    paths: list[str] = []
    clock = [0.0]
    monkeypatch.setattr(
        collector_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def fake_post(url, payload, token, *, timeout=5.0):
        paths.append(urlsplit(url).path)
        if url.endswith(TOKEN_EXCHANGE_PATH):
            return 200, {"access_token": "collector-token", "expires_in": 3600}
        if url.endswith(RUN_REGISTRATION_PATH):
            return status, {"detail": "rejected"}
        raise AssertionError("events sent for a run that was not registered")

    monkeypatch.setattr(collector_module, "_http_post", fake_post)
    delivery = make_delivery(
        spool, registration, development_collector_url="http://127.0.0.1:8080"
    )
    assert not delivery.flush_once()
    assert paths.count(RUN_REGISTRATION_PATH) == 1
    clock[0] += MAX_RETRY_SECONDS + 1
    assert not delivery.flush_once()
    assert spool.has_pending()
    if retried:
        assert paths.count(RUN_REGISTRATION_PATH) == 2
        assert spool.pending_runs() == [registration]
        assert _local_only_reason(spool_path, registration) is None
        assert "local-only" not in capsys.readouterr().err
        return
    assert paths.count(RUN_REGISTRATION_PATH) == 1
    assert spool.pending_runs() == []
    assert (
        _local_only_reason(spool_path, registration) == LOCAL_ONLY_REJECTED_REGISTRATION
    )
    assert capsys.readouterr().err.count("local-only for this run") == 1


def test_only_a_settled_client_error_is_permanent() -> None:
    """Over every status: permanent exactly for a 4xx other than a timeout and
    a rate limit. 401 and 403 count here too; the delivery answers them first."""
    for status in range(100, 600):
        expected = status // 100 == 4 and status not in (408, 429)
        assert collector_module._permanent_client_error(status) is expected, status


@pytest.mark.parametrize("refused", ["registration", "events"])
def test_every_client_error_has_one_outcome(
    tmp_path, monkeypatch, capsys, refused
) -> None:
    """Every 4xx, on either request. 401 is about the collector token, which is
    exchanged again; 403 is about the login; 408 and 429 wait and retry; any
    other is the collector's settled answer about the run."""
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-member")
    clock = [0.0]
    monkeypatch.setattr(
        collector_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    settled_reason = (
        LOCAL_ONLY_REJECTED_REGISTRATION
        if refused == "registration"
        else LOCAL_ONLY_REJECTED_EVENTS
    )
    login_reason = (
        LOCAL_ONLY_REJECTED_REGISTRATION
        if refused == "registration"
        else LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION
    )
    for status in range(400, 500):
        registration = _registration(f"run-{status}")
        spool.register(registration)
        queued = spool.append(registration, _event())
        refused_path = (
            RUN_REGISTRATION_PATH
            if refused == "registration"
            else f"/v1/runs/run-{status}/events"
        )
        sent: list[str] = []

        def fake_post(url, payload, token, *, timeout=5.0, status=status, sent=sent):
            path = urlsplit(url).path
            sent.append(path)
            if path == TOKEN_EXCHANGE_PATH:
                return 200, {"access_token": "collector-token", "expires_in": 3600}
            if path == RUN_REGISTRATION_PATH and refused == "events":
                return 201, {"registered": True}
            return status, {"detail": "refused"}

        monkeypatch.setattr(collector_module, "_http_post", fake_post)
        delivery = CollectorDelivery(
            spool, registration, development_collector_url="http://127.0.0.1:8080"
        )
        try:
            assert not delivery.flush_once(), status
            assert sent.count(refused_path) == 1, status
            # A second pass with no time passed: only a retry delay or a
            # local-only run keeps it from asking again.
            first = len(sent)
            assert not delivery.flush_once(), status
            again = sent[first:]
            reason = _local_only_reason(spool_path, registration)
            warnings = capsys.readouterr().err
            if status == 401:
                assert again[0] == TOKEN_EXCHANGE_PATH, status
                assert reason is None, status
                assert warnings == "", status
                continue
            assert again == [], (status, again)
            if status in (408, 429):
                assert reason is None, status
                assert warnings == "", status
                clock[0] += MAX_RETRY_SECONDS + 1
                assert not delivery.flush_once(), status
                assert sent[first:] == [refused_path], status
                continue
            assert reason == (login_reason if status == 403 else settled_reason), status
            assert warnings.count("local-only for this run") == 1, status
            if refused == "events" and status != 403:
                assert f"HTTP {status}" in warnings, status
            # Nothing is sent again, however long the service waits.
            clock[0] += MAX_RETRY_SECONDS + 1
            assert not delivery.flush_once(), status
            assert sent[first:] == [], status
        finally:
            delivery.close()
            # Leave nothing pending for the next status's service to adopt.
            spool.acknowledge([queued["event_id"]])


@pytest.mark.parametrize("refused", [RUN_REGISTRATION_PATH, "/v1/runs/run-a/events"])
def test_an_expired_collector_token_is_exchanged_again_not_made_local_only(
    tmp_path, monkeypatch, capsys, make_delivery, refused
) -> None:
    """A 401 is a 4xx too, but it is about the short-lived collector token,
    which a new exchange replaces."""
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "_huggingface_token", lambda: "hf-member")
    requests: list[tuple[str, str]] = []

    def fake_post(url, payload, token, *, timeout=5.0):
        path = urlsplit(url).path
        requests.append((path, token))
        if path == TOKEN_EXCHANGE_PATH:
            exchanges = sum(1 for sent, _ in requests if sent == TOKEN_EXCHANGE_PATH)
            return 200, {"access_token": f"collector-{exchanges}", "expires_in": 3600}
        if path == refused and token == "collector-1":
            return 401, {"detail": "Collector credential is invalid."}
        if path == RUN_REGISTRATION_PATH:
            return 201, {"registered": True}
        return 202, {"accepted": 1, "duplicates": 0}

    monkeypatch.setattr(collector_module, "_http_post", fake_post)
    delivery = make_delivery(
        spool, registration, development_collector_url="http://127.0.0.1:8080"
    )

    assert not delivery.flush_once()
    assert delivery.flush_once()

    assert [token for path, token in requests if path == refused] == [
        "collector-1",
        "collector-2",
    ]
    assert not spool.has_pending()
    assert _local_only_reason(spool_path, registration) is None
    assert "local-only" not in capsys.readouterr().err


def test_identity_provider_outage_keeps_events_eligible_for_retry(
    tmp_path, monkeypatch, make_delivery
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

    assert not make_delivery(spool, registration).flush_once()
    assert spool.pending_runs() == [registration]


def test_missing_credential_never_contacts_collector_and_stays_local_only(
    tmp_path, monkeypatch, make_delivery
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

    delivery = make_delivery(spool, registration)
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

import errno
import json
import os
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from given_token import (
    GIVEN_COLLECTOR_TOKEN,
    GIVEN_TOKEN_VARIABLES,
    given_token_variables,
    set_variables,
)
from huggingface_hub.errors import HfHubHTTPError
from sqlalchemy.orm import Session

from microcosm_emitter.core import auth as auth_module
from microcosm_emitter.core.auth import CollectorSession, _development_collector_url
from microcosm_emitter.core.constants import (
    PRODUCTION_COLLECTOR_URL,
)
from microcosm_emitter.core.database import (
    create_spool_engine,
)
from microcosm_emitter.core.migrations import (
    alembic_config,
    current_database_revision,
    migration_head_revision,
)
from microcosm_emitter.core.models import (
    TelemetryRunRecord,
    serialized_json_length,
)
from microcosm_emitter.host.contracts import ModuleContext
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
from microcosm_emitter.telemetry.service.module import TelemetryModule
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


DEVELOPMENT_COLLECTOR_URL = "http://127.0.0.1:8080"


def _no_hugging_face_credential(monkeypatch) -> None:
    def refuse() -> str:
        error_message = "read the Hugging Face credential"
        raise AssertionError(error_message)

    monkeypatch.setattr(collector_module, "huggingface_token", refuse)
    monkeypatch.setattr(auth_module, "huggingface_token", refuse)
    monkeypatch.setattr(auth_module, "get_token", refuse)


#: Collector answers that are not valid HTTP. http.client raises an
#: HTTPException for each, which is not an OSError.
MALFORMED_ANSWERS = {
    # Ends inside its first chunk: IncompleteRead.
    "truncated chunked answer": (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b'Transfer-Encoding: chunked\r\n\r\n40\r\n{"accepted"'
    ),
    # BadStatusLine.
    "answer that is not HTTP": b"not an HTTP answer\r\n",
}


def _telemetry_module(tmp_path, collector_url: str) -> TelemetryModule:
    """Start a telemetry module for one run, as the host starts it."""
    module = TelemetryModule(
        {
            "registration": _registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "development_collector_url": collector_url,
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    return module


def _run_state(spool: EventSpool, run_id: str = "run-a") -> tuple[str, str | None]:
    """Return the run's upload state and the reason it records."""
    with Session(spool._engine) as session:
        record = session.get(TelemetryRunRecord, (run_id, "producer-a"))
        return record.upload_state, record.local_only_reason


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
    assert _development_collector_url("http://127.0.0.1:8080") == (
        "http://127.0.0.1:8080"
    )
    for value in ("https://collector.example", "http://192.0.2.1:8080"):
        with pytest.raises(ValueError, match="loopback"):
            _development_collector_url(value)


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

        # Overrides BaseHTTPRequestHandler.log_message, which names this
        # parameter format.
        def log_message(self, format, *args):  # noqa: A002
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
        with (
            engine.begin() as connection,
            alembic_config(connection=connection) as config,
        ):
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

    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf-secret")

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


def test_a_given_collector_token_replaces_the_hugging_face_exchange(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    requests: list[tuple[str, str]] = []
    set_variables(monkeypatch, given_token_variables(DEVELOPMENT_COLLECTOR_URL))
    _no_hugging_face_credential(monkeypatch)

    def fake_post(url, payload, token, *, timeout=5.0):
        requests.append((url.removeprefix(DEVELOPMENT_COLLECTOR_URL), token))
        return (201, {}) if url.endswith("/v1/runs") else (202, {})

    monkeypatch.setattr(collector_module, "_http_post", fake_post)
    try:
        delivery = CollectorDelivery(
            spool, development_collector_url=DEVELOPMENT_COLLECTOR_URL
        )
        assert delivery.flush_once()
        assert requests == [
            ("/v1/runs", GIVEN_COLLECTOR_TOKEN),
            ("/v1/runs/run-a/events", GIVEN_COLLECTOR_TOKEN),
        ]
        assert spool.batch("run-a", "producer-a") == []
        # The token only writes telemetry: a session for another scope, such
        # as collector submissions, has no credential rather than reading the
        # Hugging Face one.
        submissions = CollectorSession(
            DEVELOPMENT_COLLECTOR_URL, scope="artifacts:submit"
        )
        assert submissions.credential() == ("missing", None)
    finally:
        spool.close()


@pytest.mark.parametrize(
    "ending",
    [
        "expires within the safety margin",
        "refused at registration",
        "refused with events",
    ],
)
def test_an_unusable_given_token_leaves_the_run_for_a_later_emitter(
    tmp_path, monkeypatch, capsys, ending
) -> None:
    spool_path = tmp_path / "events.sqlite3"
    spool = EventSpool(spool_path)
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    set_variables(
        monkeypatch,
        given_token_variables(
            DEVELOPMENT_COLLECTOR_URL,
            # The collector allows no clock leeway, so the last seconds of a
            # token would get a 401: it counts as expired 30 s early.
            expires_in=timedelta(
                seconds=10
                if ending == "expires within the safety margin"
                else 18 * 3600
            ),
        ),
    )
    _no_hugging_face_credential(monkeypatch)
    requests: list[str] = []

    def collector_refusing_the_token(url, payload, token, *, timeout=5.0):
        requests.append(url.removeprefix(DEVELOPMENT_COLLECTOR_URL))
        registering = url.endswith("/v1/runs")
        if ending == "refused at registration" or (
            ending == "refused with events" and not registering
        ):
            return 401, {"detail": "invalid token"}
        return (201, {}) if registering else (202, {})

    monkeypatch.setattr(collector_module, "_http_post", collector_refusing_the_token)
    delivery = CollectorDelivery(
        spool, development_collector_url=DEVELOPMENT_COLLECTOR_URL
    )
    for _ in range(3):
        assert not delivery.flush_once()

    # Pending, not local-only: the token cannot be renewed here, and is not
    # sent again once refused.
    with Session(spool._engine) as session:
        record = session.get(TelemetryRunRecord, ("run-a", "producer-a"))
        assert (record.upload_state, record.local_only_reason) == (
            "pending",
            "collector_token_expired"
            if ending == "expires within the safety margin"
            else "collector_token_rejected",
        )
    assert spool.pending_runs() == [registration]
    assert (
        len(requests)
        == {
            "expires within the safety margin": 0,
            "refused at registration": 1,
            "refused with events": 2,
        }[ending]
    )
    assert capsys.readouterr().err.count("collector token") == 1
    spool.close()

    # Later, an emitter with the operator's Hugging Face login delivers it.
    for name in GIVEN_TOKEN_VARIABLES:
        monkeypatch.delenv(name)
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf_member")

    def collector(url, payload, token, *, timeout=5.0):
        if url.endswith("/exchange"):
            return 200, {"access_token": "operator-session", "expires_in": 3600}
        assert token == "operator-session"
        return (201, {}) if url.endswith("/v1/runs") else (202, {})

    monkeypatch.setattr(collector_module, "_http_post", collector)
    reopened = EventSpool(spool_path)
    try:
        assert CollectorDelivery(
            reopened, development_collector_url=DEVELOPMENT_COLLECTOR_URL
        ).flush_once()
        assert not reopened.has_pending()
        with Session(reopened._engine) as session:
            record = session.get(TelemetryRunRecord, ("run-a", "producer-a"))
            assert (record.upload_state, record.local_only_reason) == (
                "pending",
                None,
            )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("credential", "warning"),
    [
        ("hf-outsider", "not accepted as a PolicyEngine organization member"),
        ("hf-member with a pasted space", "contains characters that cannot be sent"),
    ],
    ids=["outside the organization", "not sendable as a bearer token"],
)
def test_a_refused_or_unsendable_credential_keeps_events_local(
    tmp_path, monkeypatch, capsys, credential, warning
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: credential)

    def collector_refusing_outsiders(url, payload, token, *, timeout=5.0):
        requests.append(payload)
        if token == "hf-outsider":
            return 403, {"detail": "not a member"}
        # The real request, which refuses a credential it cannot send.
        return auth_module._http_post(url, payload, token, timeout=timeout)

    monkeypatch.setattr(collector_module, "_http_post", collector_refusing_outsiders)
    delivery = CollectorDelivery(
        spool,
        development_collector_url="http://127.0.0.1:8080",
    )

    # Neither stops delivery: the run's events stay recorded, local-only, as
    # for any credential the collector refuses.
    assert not delivery.flush_once()
    assert not delivery.flush_once()
    assert spool.has_pending()
    assert spool.pending_runs() == []
    with Session(spool._engine) as session:
        record = session.get(TelemetryRunRecord, ("run-a", "producer-a"))
        assert (record.upload_state, record.local_only_reason) == (
            "local_only",
            "huggingface_credential_rejected",
        )
    # An unsendable credential is never sent.
    assert requests == ([{}] if credential == "hf-outsider" else [])
    # One warning, giving the cause.
    standard_error = capsys.readouterr().err
    assert standard_error.count("local-only for this run") == 1
    assert warning in standard_error


def test_identity_provider_outage_keeps_events_eligible_for_retry(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf-member")
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
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: None)
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
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf-later")
    assert reopened.pending_runs() == []


@pytest.mark.parametrize(
    "unreadable_login",
    [
        PermissionError(13, "Permission denied", "/synthetic/hf/token"),
        UnicodeDecodeError("utf-8", b"\xffsynthetic", 0, 1, "invalid start byte"),
    ],
    ids=["not readable by this user", "not text"],
)
def test_an_unreadable_saved_login_keeps_runs_pending_until_it_can_be_read(
    tmp_path, monkeypatch, capsys, unreadable_login
) -> None:
    # An earlier build's run, still pending in the event queue file.
    spool_path = tmp_path / "events.sqlite3"
    earlier = _registration("run-earlier")
    earlier_queue = EventSpool(spool_path)
    earlier_queue.register(earlier)
    earlier_queue.append(earlier, _event())
    earlier_queue.close()
    # Neither HF_TOKEN nor HUGGINGFACE_TOKEN is set, so the emitter reads the
    # login saved by `hf auth login`.
    login_reads: list[str] = []

    def read_saved_login() -> str:
        login_reads.append("read")
        raise unreadable_login

    def refuse_request(*arguments, **options):
        error_message = "sent a request without a credential"
        raise AssertionError(error_message)

    monkeypatch.setattr(auth_module, "get_token", read_saved_login)
    monkeypatch.setattr(auth_module, "_http_post", refuse_request)
    monkeypatch.setattr(collector_module, "_http_post", refuse_request)
    real_monotonic = time.monotonic
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    module = _telemetry_module(tmp_path, DEVELOPMENT_COLLECTOR_URL)
    try:
        # The host stops a module whose tick raises.
        module.tick(clock[0])

        # Both runs stay pending, saying why; neither becomes local-only.
        assert len(module.spool.pending_runs()) == 2
        for run_id in ("run-a", "run-earlier"):
            assert _run_state(module.spool, run_id) == (
                "pending",
                "huggingface_login_unreadable",
            )

        # The login is read again only after the backoff.
        reads = len(login_reads)
        module.tick(clock[0])
        assert len(login_reads) == reads
        clock[0] += 100.0
        module.tick(clock[0])
        assert len(login_reads) > reads
    finally:
        monkeypatch.setattr(collector_module.time, "monotonic", real_monotonic)
        started = time.monotonic()
        module.close(started + 5)
    # Nothing can be delivered from here, so the shutdown drain ends at once.
    assert time.monotonic() - started < 1
    warning = capsys.readouterr().err
    assert warning.count("Microcosm telemetry is waiting") == 1
    # Only the error's type: its text names the path or quotes the file.
    assert f"could not be read ({type(unreadable_login).__name__})" in warning
    assert "synthetic" not in warning
    assert "0xff" not in warning

    # Once the login can be read, the next emitter delivers both runs.
    def collector(url, payload, token, *, timeout=5.0):
        if url.endswith("/exchange"):
            return 200, {"access_token": "synthetic-session", "expires_in": 3600}
        assert token == "synthetic-session"
        return (201, {}) if url.endswith("/v1/runs") else (202, {})

    monkeypatch.setattr(auth_module, "get_token", lambda: "hf_synthetic_member")
    monkeypatch.setattr(collector_module, "_http_post", collector)
    reopened = EventSpool(spool_path)
    try:
        assert CollectorDelivery(
            reopened, development_collector_url=DEVELOPMENT_COLLECTOR_URL
        ).flush_once()
        assert not reopened.has_pending()
        for run_id in ("run-a", "run-earlier"):
            assert _run_state(reopened, run_id) == ("pending", None)
    finally:
        reopened.close()


def test_delivery_resumes_once_the_saved_login_can_be_read(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    saved_login: list[str | Exception] = [
        PermissionError(13, "Permission denied", "/synthetic/hf/token")
    ]

    def read_saved_login() -> str:
        if isinstance(saved_login[0], Exception):
            raise saved_login[0]
        return saved_login[0]

    def collector(url, payload, token, *, timeout=5.0):
        if url.endswith("/exchange"):
            return 200, {"access_token": "synthetic-session", "expires_in": 3600}
        return (201, {}) if url.endswith("/v1/runs") else (202, {})

    monkeypatch.setattr(auth_module, "get_token", read_saved_login)
    monkeypatch.setattr(collector_module, "_http_post", collector)
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    delivery = CollectorDelivery(
        spool, development_collector_url=DEVELOPMENT_COLLECTOR_URL
    )
    try:
        assert not delivery.flush_once()
        assert delivery.waiting_for_credential

        # The user repairs the file, and the next attempt delivers the run.
        saved_login[0] = "hf_synthetic_member"
        clock[0] += 100.0
        assert delivery.flush_once()
        assert not spool.has_pending()
        assert _run_state(spool) == ("pending", None)
        # So the shutdown drain waits for deliveries again.
        assert not delivery.waiting_for_credential
    finally:
        spool.close()


def test_a_registered_run_loses_the_unreadable_login_reason_once_it_can_be_read(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    saved_login: list[str | Exception] = ["hf_synthetic_member"]

    def read_saved_login() -> str:
        if isinstance(saved_login[0], Exception):
            raise saved_login[0]
        return saved_login[0]

    def collector(url, payload, token, *, timeout=5.0):
        if url.endswith("/exchange"):
            return 200, {"access_token": "synthetic-session", "expires_in": 3600}
        return (201, {}) if url.endswith("/v1/runs") else (202, {})

    monkeypatch.setattr(auth_module, "get_token", read_saved_login)
    monkeypatch.setattr(collector_module, "_http_post", collector)
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    delivery = CollectorDelivery(
        spool, development_collector_url=DEVELOPMENT_COLLECTOR_URL
    )
    try:
        # This emitter registers the run and delivers its first event.
        assert delivery.flush_once()
        spool.append(registration, _event("load_inputs"))

        # When the exchanged token is renewed, the login cannot be read.
        saved_login[0] = PermissionError(13, "Permission denied", "/synthetic/hf/token")
        clock[0] += 3600.0
        assert not delivery.flush_once()
        assert _run_state(spool) == ("pending", "huggingface_login_unreadable")

        # Once it can be read, the reason goes with the delivery.
        saved_login[0] = "hf_synthetic_member"
        clock[0] += 100.0
        assert delivery.flush_once()
        assert not spool.has_pending()
        assert _run_state(spool) == ("pending", None)
    finally:
        spool.close()


def test_a_collector_outage_after_the_login_can_be_read_is_retried_at_shutdown(
    tmp_path, monkeypatch
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    registration = _registration()
    spool.register(registration)
    spool.append(registration, _event())
    saved_login: list[str | Exception] = [
        PermissionError(13, "Permission denied", "/synthetic/hf/token")
    ]

    def read_saved_login() -> str:
        if isinstance(saved_login[0], Exception):
            raise saved_login[0]
        return saved_login[0]

    monkeypatch.setattr(auth_module, "get_token", read_saved_login)
    monkeypatch.setattr(
        collector_module,
        "_http_post",
        lambda *arguments, **options: (503, {"detail": "temporarily unavailable"}),
    )
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    delivery = CollectorDelivery(
        spool, development_collector_url=DEVELOPMENT_COLLECTOR_URL
    )
    try:
        assert not delivery.flush_once()
        assert delivery.waiting_for_credential

        # The login can be read again, but the collector does not answer.
        saved_login[0] = "hf_synthetic_member"
        clock[0] += 100.0
        assert not delivery.flush_once()

        # So the shutdown drain retries, as for any outage, and the run no
        # longer says it waits for the login.
        assert not delivery.waiting_for_credential
        assert _run_state(spool) == ("pending", None)
        assert spool.pending_runs() == [registration]
    finally:
        spool.close()


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "Permission denied", "/synthetic/hf/token"),
        IsADirectoryError(errno.EISDIR, "Is a directory", "/synthetic/hf/token"),
        OSError(
            errno.ELOOP, "Too many levels of symbolic links", "/synthetic/hf/token"
        ),
        UnicodeDecodeError("utf-8", b"\xffsynthetic", 0, 1, "invalid start byte"),
    ],
    ids=[
        "not readable by this user",
        "a directory",
        "a symbolic link loop",
        "not text",
    ],
)
def test_a_saved_login_that_cannot_be_read_raises_an_error_without_its_details(
    monkeypatch, error
) -> None:
    def get_token() -> str:
        raise error

    monkeypatch.setattr(auth_module, "get_token", get_token)

    with pytest.raises(auth_module.UnreadableLoginError) as raised:
        auth_module.huggingface_token()

    unreadable = raised.value
    assert unreadable.error_type == type(error).__name__
    # Nothing keeps the original error, not even as context: its text names
    # the file, and a UnicodeDecodeError holds the file's bytes.
    assert unreadable.__context__ is None
    assert unreadable.__cause__ is None
    assert "synthetic" not in str(unreadable)


def _hub_outage() -> HfHubHTTPError:
    """Return the error huggingface_hub raises for a 503 from the Hub, an OSError."""
    request = httpx.Request("POST", "https://huggingface.co/oauth/token")
    return HfHubHTTPError(
        "503 Server Error: Service Unavailable",
        response=httpx.Response(503, request=request),
    )


@pytest.mark.parametrize(
    ("oidc_resource", "error"),
    [
        ("datasets/policyengine/synthetic", _hub_outage()),
        (
            "datasets/policyengine/synthetic",
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ),
        (None, _hub_outage()),
    ],
    ids=[
        "Hub outage during the OIDC exchange",
        "OIDC answer that is not text",
        "Hub outage without OIDC",
    ],
)
def test_only_a_saved_login_that_cannot_be_read_counts_as_unreadable(
    monkeypatch, oidc_resource, error
) -> None:
    # With HF_OIDC_RESOURCE set, huggingface_hub exchanges a CI OIDC token
    # instead of reading the saved login. Its errors, and any OSError that
    # does not come from reading a file, propagate as before.
    if oidc_resource is not None:
        monkeypatch.setenv("HF_OIDC_RESOURCE", oidc_resource)

    def get_token() -> str:
        raise error

    monkeypatch.setattr(auth_module, "get_token", get_token)

    with pytest.raises(type(error)) as raised:
        auth_module.huggingface_token()

    assert raised.value is error


@pytest.mark.parametrize("endpoint", ["registration", "events"])
@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_permanent_collector_rejection_retains_only_rejected_producer(
    tmp_path,
    monkeypatch,
    capsys,
    endpoint,
    status,
):
    spool = EventSpool(tmp_path / "rejected.sqlite3")
    bad = _registration()
    good = {**bad, "producer_id": "producer-good"}
    spool.register(bad)
    spool.register(good)
    rejected = spool.append(bad, _event())
    spool.append(good, _event())
    requests = []
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf_test_member")

    def post(url, payload, token):
        requests.append((url, payload))
        if url.endswith("/exchange"):
            return 200, {"access_token": "synthetic", "expires_in": 3600}
        if url.endswith("/v1/runs"):
            if (
                endpoint == "registration"
                and payload["producer_id"] == bad["producer_id"]
            ):
                return status, {}
            return 201, {}
        if (
            endpoint == "events"
            and payload["events"][0]["producer_id"] == bad["producer_id"]
        ):
            return status, {}
        return 202, {}

    monkeypatch.setattr(collector_module, "_http_post", post)
    delivery = CollectorDelivery(spool, development_collector_url="http://127.0.0.1:1")
    try:
        assert delivery.flush_once()  # The unrelated producer still uploads.
        assert spool.pending_runs() == []
        assert spool.batch(bad["run_id"], bad["producer_id"]) == [rejected]
        with Session(spool._engine) as session:
            record = session.get(
                TelemetryRunRecord, (bad["run_id"], bad["producer_id"])
            )
            assert record.upload_state == "local_only"
            assert record.local_only_reason == (
                "collector_rejected_events"
                if endpoint == "events"
                else "run_registration_rejected"
            )
        request_count = len(requests)
        assert not delivery.flush_once()
        assert len(requests) == request_count
        warning = capsys.readouterr().err
        assert warning.count("local-only for this run") == 1
        if endpoint == "events":
            assert f"HTTP {status}" in warning
            assert "will not be retried" in warning
    finally:
        spool.close()


@pytest.mark.parametrize("endpoint", ["registration", "events"])
@pytest.mark.parametrize("status", [408, 429, 503, None])
def test_transient_collector_rejection_remains_retryable(
    tmp_path,
    monkeypatch,
    endpoint,
    status,
):
    spool = EventSpool(tmp_path / "retry.sqlite3")
    registration = _registration()
    spool.register(registration)
    queued = spool.append(registration, _event())
    clock = [100.0]
    attempts = []
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(collector_module, "huggingface_token", lambda: "hf_test_member")

    def post(url, payload, token):
        if url.endswith("/exchange"):
            return 200, {"access_token": "synthetic", "expires_in": 3600}
        current_endpoint = "registration" if url.endswith("/v1/runs") else "events"
        if current_endpoint == endpoint:
            attempts.append(current_endpoint)
            if len(attempts) == 1:
                if status is None:
                    error_message = "synthetic transient network failure"
                    raise OSError(error_message)
                return status, {}
        return (201, {}) if current_endpoint == "registration" else (202, {})

    monkeypatch.setattr(collector_module, "_http_post", post)
    delivery = CollectorDelivery(spool, development_collector_url="http://127.0.0.1:1")
    try:
        assert not delivery.flush_once()
        assert spool.pending_runs() == [registration]
        assert spool.batch(registration["run_id"], registration["producer_id"]) == [
            queued
        ]
        assert not delivery.flush_once()  # Respect the retry deadline.
        assert len(attempts) == 1
        clock[0] += 100.0
        assert delivery.flush_once()
        assert len(attempts) == 2
        assert not spool.has_pending()
    finally:
        spool.close()


@pytest.mark.parametrize("malformed_answer", sorted(MALFORMED_ANSWERS))
@pytest.mark.parametrize("endpoint", ["registration", "events"])
def test_a_malformed_collector_answer_is_retried_like_a_network_failure(
    tmp_path, monkeypatch, endpoint, malformed_answer
) -> None:
    malformed_path = (
        "/v1/runs" if endpoint == "registration" else "/v1/runs/run-a/events"
    )
    paths: list[str] = []

    class Collector(BaseHTTPRequestHandler):
        """Answers the first request to ``malformed_path`` with broken HTTP."""

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            if self.path.endswith("/exchange"):
                return self._json(
                    200, {"access_token": "synthetic-session", "expires_in": 3600}
                )
            if self.path == malformed_path and paths.count(self.path) == 1:
                self.wfile.write(MALFORMED_ANSWERS[malformed_answer])
                return None
            self._json(201 if self.path == "/v1/runs" else 202, {})
            return None

        def _json(self, status: int, document: dict[str, object]) -> None:
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Overrides BaseHTTPRequestHandler.log_message, which names this
        # parameter format.
        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("HF_TOKEN", "hf_synthetic_member")
    module = _telemetry_module(tmp_path, f"http://127.0.0.1:{server.server_port}")
    try:
        # The host stops a module whose tick raises.
        module.tick(clock[0])
        assert paths.count(malformed_path) == 1

        # The run and its queued event stay as they were.
        assert module.spool.pending_runs() == [module.registration]
        assert _run_state(module.spool) == ("pending", None)
        assert len(module.spool.batch("run-a", "producer-a")) == 1

        # Retried after the backoff, as for any network failure.
        module.tick(clock[0])
        assert paths.count(malformed_path) == 1
        clock[0] += 100.0
        module.tick(clock[0])
        assert paths.count(malformed_path) == 2
        assert not module.spool.has_pending()
    finally:
        module.close(clock[0])
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()


def test_a_collector_token_that_cannot_be_sent_is_retried_like_an_outage(
    tmp_path, monkeypatch
) -> None:
    exchanges: list[str] = []
    sent: list[str] = []

    def collector_issuing_a_token_with_a_space(url, payload, token, *, timeout=5.0):
        exchanges.append(url)
        return 200, {"access_token": "session token", "expires_in": 3600}

    # The real request, which refuses a token it cannot send with ValueError.
    real_post = collector_module._http_post

    def send(url, payload, token, *, timeout=5.0):
        sent.append(url)
        return real_post(url, payload, token, timeout=timeout)

    monkeypatch.setattr(
        auth_module, "_http_post", collector_issuing_a_token_with_a_space
    )
    monkeypatch.setattr(collector_module, "_http_post", send)
    monkeypatch.setenv("HF_TOKEN", "hf_synthetic_member")
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock[0])
    module = _telemetry_module(tmp_path, DEVELOPMENT_COLLECTOR_URL)
    try:
        # The host stops a module whose tick raises.
        module.tick(clock[0])

        # The session does not keep the token, so nothing is sent with it,
        # and the run and its queued event stay as they were.
        assert sent == []
        assert module.spool.pending_runs() == [module.registration]
        assert _run_state(module.spool) == ("pending", None)
        assert len(module.spool.batch("run-a", "producer-a")) == 1

        # The exchange is tried again after the backoff, as after an outage.
        module.tick(clock[0])
        assert len(exchanges) == 1
        clock[0] += 100.0
        module.tick(clock[0])
        assert len(exchanges) == 2
        assert sent == []
    finally:
        module.close(clock[0])


def test_resource_sampler_keeps_reaped_child_cpu_monotonic(monkeypatch) -> None:
    state = {"child_alive": True}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return float(self.pid)

        def children(self, *, recursive=False):
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

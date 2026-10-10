"""What a telemetry service sends the collector, and how it takes three answers.

The fake collector here answers as the hosted one does
(PolicyEngine/calibration-diagnostics, ``telemetry-service``): a request body
over 1 MiB gets 413 before anything in it is read; events for a run it does not
have get 404; registering a run is repeatable for the Hugging Face user who owns
it; another user gets 403, and the owner gets 409 for other metadata.

Invariants, each checked below for a service delivering its own run:

- A warning says what the collector refused. A refused registration names its
  HTTP status, a 403 names the Hugging Face user and not organization
  membership, and only a refused token exchange says the credential was not
  accepted.
- An events request is never empty and never over 1 MiB unless its only event
  is. Requests carry a run's events in sequence order with none skipped or
  repeated, and each is the longest run of the next queued events that fits.
- A run the collector answers 404 for is registered again once; a second 404
  with no batch acknowledged in between makes the run local-only. So between
  two registrations of a run there is always a 404, and from the second
  registration on, an acknowledged batch as well.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.protocol import (
    MAX_TELEMETRY_DETAILS_BYTES,
    MAX_TELEMETRY_MESSAGE_CHARS,
)
from microcosm_emitter.telemetry.service import collector as collector_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.constants import (
    BATCH_SIZE,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_EVENTS,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_EVENTS_REQUEST_BYTES,
    MAX_RETRY_SECONDS,
    RUN_REGISTRATION_PATH,
    TOKEN_EXCHANGE_PATH,
)
from microcosm_emitter.telemetry.service.database import create_spool_engine
from microcosm_emitter.telemetry.service.models import TelemetryRunRecord
from microcosm_emitter.telemetry.service.sanitization import (
    sanitize_details,
    sanitize_text,
)
from microcosm_emitter.telemetry.service.spool import EventSpool

DEVELOPMENT_COLLECTOR = "http://127.0.0.1:8080"
#: The hosted collector's body limit, written out here and not imported: its
#: ``RequestBodyLimitMiddleware(max_bytes=1_048_576)`` refuses anything larger.
COLLECTOR_BODY_LIMIT = 1_048_576
#: The longest identifier the collector's schema accepts.
LONGEST_IDENTIFIER = "r" * 192
RESOURCES = {
    "cpu_user_seconds": 123456.789012,
    "cpu_system_seconds": 123456.789012,
    "rss_bytes": 2**40,
    "peak_rss_bytes": 2**40,
}
SMALL_EVENT = {
    "timestamp": "2026-10-09T00:00:00+00:00",
    "event_type": "stage",
    "stage_id": "compile",
    "status": "started",
    "message": None,
    "details": {},
}


def _registration(run_id: str = "run-a", producer_id: str = "producer-a") -> dict:
    return TelemetryRun(
        run_id=run_id,
        country_code="US",
        pipeline="collector-delivery",
        producer_id=producer_id,
    ).as_registration()


class FakeCollector:
    """The hosted collector's answers to one member's requests."""

    def __init__(self) -> None:
        self.runs: set[str] = set()
        self.body_limit = COLLECTOR_BODY_LIMIT
        # Whether a registration it answers 201 is kept. Off: a collector that
        # accepts a run and still does not have it.
        self.keeps_runs = True
        # A status to answer a kind of request with, instead of its own.
        self.refuse: dict[str, int] = {}
        self.accepted: list[dict[str, Any]] = []
        # ("exchange" | "register" | "events", status), in the order answered.
        self.answers: list[tuple[str, int]] = []
        # (body bytes, sequences) of every events request, whatever the answer.
        self.event_requests: list[tuple[int, list[int]]] = []

    def post(self, url, payload, token, *, timeout=5.0) -> tuple[int, dict]:
        path = urlsplit(url).path
        if path == TOKEN_EXCHANGE_PATH:
            if "exchange" in self.refuse:
                return self._answer("exchange", self.refuse["exchange"])
            body = {"access_token": "collector-token", "expires_in": 3600}
            return self._answer("exchange", 200, body)
        kind = "register" if path == RUN_REGISTRATION_PATH else "events"
        size = len(collector_module._request_body(payload))
        if kind == "events":
            sequences = [event["sequence"] for event in payload["events"]]
            self.event_requests.append((size, sequences))
        if size > self.body_limit:
            return self._answer(kind, 413)
        if kind in self.refuse:
            return self._answer(kind, self.refuse[kind])
        if kind == "register":
            if self.keeps_runs:
                self.runs.add(payload["run_id"])
            return self._answer(kind, 201, {"registered": True})
        if path.split("/")[3] not in self.runs:
            return self._answer(kind, 404)
        self.accepted.extend(payload["events"])
        return self._answer(kind, 202, {"accepted": len(payload["events"])})

    def _answer(self, kind: str, status: int, body: dict | None = None):
        self.answers.append((kind, status))
        return status, body if body is not None else {"detail": "refused"}

    def asked(self, kind: str) -> list[int]:
        return [status for what, status in self.answers if what == kind]


@dataclass
class Service:
    """One build's delivery, its spool, and a clock the test moves."""

    spool: EventSpool
    registration: dict[str, Any]
    delivery: CollectorDelivery
    clock: SimpleNamespace
    queued: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: Mapping[str, Any] = SMALL_EVENT, count: int = 1) -> None:
        for _ in range(count):
            self.queued.append(
                self.spool.append(self.registration, event, resources=RESOURCES)
            )

    def flush(self) -> bool:
        return self.delivery.flush_once()

    def flush_until_idle(self, limit: int = 50) -> None:
        for _ in range(limit):
            if not self.flush():
                return
        raise AssertionError("delivery never went idle")

    def wait_out_any_retry_delay(self) -> None:
        self.clock.now += MAX_RETRY_SECONDS + 1

    def local_only_reason(self) -> str | None:
        engine = create_spool_engine(self.spool.path)
        try:
            with Session(engine) as session:
                run = session.get(
                    TelemetryRunRecord,
                    (self.registration["run_id"], self.registration["producer_id"]),
                )
                return run.local_only_reason
        finally:
            engine.dispose()

    def pending(self) -> bool:
        return self.spool.pending_runs() == [self.registration]


_RUN_NUMBERS = itertools.count()


def _new_registration(longest: bool = False) -> dict[str, Any]:
    """A run no earlier service in this module used, so one spool serves many."""

    number = f"{next(_RUN_NUMBERS):08d}"
    if longest:
        return _registration(
            number + LONGEST_IDENTIFIER[len(number) :],
            number + "p" * (len(LONGEST_IDENTIFIER) - len(number)),
        )
    return _registration(f"run-{number}", f"producer-{number}")


@contextmanager
def _service(
    spool: EventSpool,
    collector: FakeCollector,
    registration: dict[str, Any] | None = None,
) -> Iterator[Service]:
    """Run one service on ``spool``, leaving none of its events queued after.

    A test that runs many services in turn gives each a new run on one spool;
    a run left with events would be an orphan for the next service to adopt.
    """

    registration = registration or _new_registration()
    spool.register(registration)
    clock = SimpleNamespace(now=0.0)
    with (
        mock.patch.object(collector_module, "_huggingface_token", lambda: "hf-member"),
        mock.patch.object(collector_module, "_http_post", collector.post),
        mock.patch.object(
            collector_module, "time", SimpleNamespace(monotonic=lambda: clock.now)
        ),
    ):
        delivery = CollectorDelivery(
            spool, registration, development_collector_url=DEVELOPMENT_COLLECTOR
        )
        service = Service(spool, registration, delivery, clock)
        try:
            yield service
        finally:
            delivery.close()
            spool.acknowledge([event["event_id"] for event in service.queued])


@pytest.fixture
def collector() -> FakeCollector:
    return FakeCollector()


@pytest.fixture
def spool(tmp_path) -> Iterator[EventSpool]:
    spool = EventSpool(tmp_path / "events.sqlite3")
    yield spool
    spool._engine.dispose()


@pytest.fixture
def service(spool, collector) -> Iterator[Service]:
    with _service(spool, collector) as running:
        yield running


# --- What a warning says -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "warning"),
    [
        pytest.param(
            409,
            "Microcosm telemetry is local-only for this run: the collector rejected "
            "its registration (HTTP 409), so it will not be retried. The dataset "
            "build will continue.\n",
            id="409",
        ),
        pytest.param(
            422,
            "Microcosm telemetry is local-only for this run: the collector rejected "
            "its registration (HTTP 422), so it will not be retried. The dataset "
            "build will continue.\n",
            id="422",
        ),
        pytest.param(
            403,
            "Microcosm telemetry is local-only for this run: the collector refused "
            "the ambient Hugging Face user for it (HTTP 403). A run belongs to the "
            "user who registered it first. The dataset build will continue.\n",
            id="403",
        ),
    ],
)
def test_a_rejected_registration_warning_says_what_the_collector_refused(
    service, collector, capsys, status, warning
) -> None:
    """The collector answers 409 to the run's owner for other metadata, 422 for
    a registration outside its schema, and 403 when another Hugging Face user
    registered the run first. None of them is about organization membership,
    which the token exchange had already accepted."""
    collector.refuse["register"] = status
    service.emit()

    assert not service.flush()
    service.wait_out_any_retry_delay()
    assert not service.flush()

    assert service.local_only_reason() == LOCAL_ONLY_REJECTED_REGISTRATION
    assert collector.asked("register") == [status]
    assert capsys.readouterr().err == warning


def _expected_warning(request: str, status: int) -> str | None:
    if request == "exchange":
        return (
            "Microcosm telemetry is local-only for this run: the ambient Hugging "
            "Face credential was not accepted as a PolicyEngine organization "
            "member. The dataset build will continue.\n"
        )
    if status == 403:
        return (
            "Microcosm telemetry is local-only for this run: the collector refused "
            "the ambient Hugging Face user for it (HTTP 403). A run belongs to the "
            "user who registered it first. The dataset build will continue.\n"
        )
    what, pronoun = (
        ("registration", "it") if request == "register" else ("events", "they")
    )
    return (
        "Microcosm telemetry is local-only for this run: the collector rejected "
        f"its {what} (HTTP {status}), so {pronoun} will not be retried. The "
        "dataset build will continue.\n"
    )


@pytest.mark.parametrize("request_kind", ["exchange", "register", "events"])
def test_every_refusal_prints_one_warning_about_the_request_refused(
    spool, capsys, request_kind
) -> None:
    """Over every 4xx that makes the service's own run local-only, on each of
    the three requests: exactly one warning, worded for that request. A refused
    token exchange is the only answer about the credential."""
    reasons = {
        "exchange": LOCAL_ONLY_REJECTED_CREDENTIAL,
        "register": LOCAL_ONLY_REJECTED_REGISTRATION,
        "events": LOCAL_ONLY_REJECTED_EVENTS,
    }
    statuses = (
        (401, 403)
        if request_kind == "exchange"
        else [s for s in range(400, 500) if s not in (401, 408, 429)]
    )
    for status in statuses:
        collector = FakeCollector()
        collector.refuse[request_kind] = status
        with _service(spool, collector) as service:
            service.emit()
            for _ in range(3):
                service.flush()
                service.wait_out_any_retry_delay()

            expected_reason = reasons[request_kind]
            if request_kind == "events" and status == 403:
                expected_reason = LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION
            assert service.local_only_reason() == expected_reason, status
            warning = capsys.readouterr().err
            assert warning == _expected_warning(request_kind, status), status
            if request_kind != "exchange":
                assert "credential" not in warning, status
                assert "organization member" not in warning, status


# --- How large a request is --------------------------------------------------


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


def _details(keys: int, character: str, length: int) -> dict[str, Any]:
    """Details bounded as every event's are before it is queued."""

    return sanitize_details({f"k{index}": character * length for index in range(keys)})


def _largest_details() -> dict[str, Any]:
    """Details of exactly ``MAX_TELEMETRY_DETAILS_BYTES``, the most kept whole."""

    details: dict[str, Any] = {}
    while True:
        grown = {**details, f"k{len(details)}": "x" * 190}
        if _json_bytes(grown) > MAX_TELEMETRY_DETAILS_BYTES - 16:
            break
        details = grown
    # One more key takes the object to the limit exactly: ,"pad":"…"
    padding = MAX_TELEMETRY_DETAILS_BYTES - _json_bytes(details) - len(',"pad":""')
    details["pad"] = "x" * padding
    assert _json_bytes(details) == MAX_TELEMETRY_DETAILS_BYTES
    assert sanitize_details(details) == details
    return details


def _bounded_event(message: str | None, details: Mapping[str, Any]) -> dict[str, Any]:
    """An event bounded as every event is before it is queued: the message cut
    to ``MAX_TELEMETRY_MESSAGE_CHARS`` and the details to
    ``MAX_TELEMETRY_DETAILS_BYTES``. Its identifiers are the longest the
    collector accepts."""

    return {
        "timestamp": "2026-10-09T00:00:00.000000+00:00",
        "event_type": "calibration",
        "stage_id": LONGEST_IDENTIFIER,
        "status": "progress",
        "message": (
            sanitize_text(message, limit=MAX_TELEMETRY_MESSAGE_CHARS)
            if message
            else None
        ),
        "details": sanitize_details(details),
    }


def _assert_requests_fit_and_keep_order(service: Service, collector: FakeCollector):
    """The size and order invariants, over every events request sent so far."""

    queued = service.queued
    start = 0
    for size, sequences in collector.event_requests:
        assert sequences, "an events request was empty"
        assert size <= COLLECTOR_BODY_LIMIT, (size, len(sequences))
        count = len(sequences)
        # In order, none skipped, none repeated.
        assert sequences == list(range(start + 1, start + count + 1))
        assert count <= BATCH_SIZE
        if count < BATCH_SIZE and start + count < len(queued):
            # It stopped short only because the next event would not fit.
            one_more = {"events": queued[start : start + count + 1]}
            assert len(collector_module._request_body(one_more)) > COLLECTOR_BODY_LIMIT
        start += count
    assert start == len(queued)
    assert [event["event_id"] for event in collector.accepted] == [
        event["event_id"] for event in queued
    ]


def test_the_limit_requests_are_cut_to_is_the_collectors() -> None:
    assert MAX_EVENTS_REQUEST_BYTES == COLLECTOR_BODY_LIMIT
    assert collector_module.MAX_EVENTS_REQUEST_BYTES == COLLECTOR_BODY_LIMIT


@pytest.mark.parametrize(
    ("over", "requests"), [(0, [[1, 2]]), (1, [[1], [2]])], ids=["exact", "one-over"]
)
def test_two_events_of_exactly_the_limit_go_together_and_a_byte_more_apart(
    service, collector, capsys, over, requests
) -> None:
    """The collector takes a body of exactly its limit and refuses one byte
    more, so that is where a request is cut."""
    service.emit()
    # The second event differs from the first only in its details, so its
    # padding sets the size of the request that would carry both.
    first = len(collector_module._request_body(service.queued[0]))
    envelope_and_comma = len('{"events":[,]}')
    padding = COLLECTOR_BODY_LIMIT + over - envelope_and_comma - 2 * first
    padding -= len('{"pad":""}') - len("{}")
    service.emit({**SMALL_EVENT, "details": {"pad": "x" * padding}})
    both = len(collector_module._request_body({"events": service.queued}))
    assert both == COLLECTOR_BODY_LIMIT + over

    service.flush_until_idle()

    assert [sequences for _, sequences in collector.event_requests] == requests
    assert set(collector.asked("events")) == {202}
    assert not service.spool.has_pending()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("character", "requests"),
    [
        # One byte a character: a full batch fits, and is still sent whole.
        ("m", 1),
        # json.dumps escapes a non-ASCII character to 6 or 12 bytes, and a full
        # batch of such events is over the limit.
        ("é", 2),
        ("\U0001f600", 2),
    ],
)
def test_a_full_batch_of_the_largest_events_is_cut_to_fit(
    spool, collector, capsys, character, requests
) -> None:
    event = _bounded_event(
        character * 2 * MAX_TELEMETRY_MESSAGE_CHARS, _largest_details()
    )
    assert len(event["message"]) == MAX_TELEMETRY_MESSAGE_CHARS
    with _service(spool, collector, _new_registration(longest=True)) as service:
        service.emit(event, BATCH_SIZE)
        whole = len(collector_module._request_body({"events": service.queued}))
        assert (whole > COLLECTOR_BODY_LIMIT) == (requests > 1), whole

        service.flush_until_idle()

        assert len(collector.event_requests) == requests
        _assert_requests_fit_and_keep_order(service, collector)
        assert not service.spool.has_pending()
        assert service.local_only_reason() is None
    assert capsys.readouterr().err == ""


MESSAGE = st.one_of(
    st.none(),
    st.tuples(
        st.sampled_from(["m", "é", "\U0001f600", "\x01", '"']),
        st.integers(1, MAX_TELEMETRY_MESSAGE_CHARS + 100),
    ),
)
#: Keys, the character their values repeat, and how long each value is.
DETAILS = st.tuples(
    st.integers(0, 60),
    st.sampled_from(["x", "é", "\U0001f600"]),
    st.integers(0, 2_100),
)
#: A few event shapes, and which of them each queued event takes.
SHAPES = st.lists(st.tuples(MESSAGE, DETAILS), min_size=1, max_size=5)
ORDER = st.lists(st.integers(0, 4), min_size=1, max_size=2 * BATCH_SIZE + 30)
LARGEST = (("\U0001f600", MAX_TELEMETRY_MESSAGE_CHARS), (60, "x", 2_100))


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(shapes=SHAPES, order=ORDER)
# Every event at its largest: 100 of them are 1.4 MiB.
@example(shapes=[LARGEST], order=[0] * (2 * BATCH_SIZE + 30))
# Large and small events alternating, so the cut falls between unequal events.
@example(shapes=[LARGEST, (None, (0, "x", 0))], order=[0, 1] * BATCH_SIZE)
def test_every_request_fits_and_events_arrive_in_order(spool, shapes, order) -> None:
    """For any queue of events within those bounds: every request body is
    within the collector's limit, and the collector receives every event, in
    sequence order, none skipped."""
    events = []
    for message, (keys, character, length) in shapes:
        text = message[0] * message[1] if message is not None else None
        events.append(_bounded_event(text, _details(keys, character, length)))
    collector = FakeCollector()
    with _service(spool, collector, _new_registration(longest=True)) as service:
        for index in order:
            service.emit(events[index % len(events)])

        service.flush_until_idle()

        assert 413 not in collector.asked("events")
        _assert_requests_fit_and_keep_order(service, collector)
        assert not service.spool.has_pending()
        assert service.local_only_reason() is None


JSON = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=20),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)
EVENTS = st.lists(st.dictionaries(st.text(max_size=8), JSON, max_size=4), max_size=12)


@settings(max_examples=400, deadline=None)
@given(events=EVENTS, limit=st.integers(0, 400))
@example(events=[{}, {}], limit=18)  # {"events":[{},{}]} is exactly 18 bytes
@example(events=[{}, {}], limit=17)
def test_the_events_sent_are_the_longest_leading_run_that_fits(events, limit) -> None:
    """For any events and any limit, against the body that would be sent."""

    def body(count: int) -> int:
        return len(collector_module._request_body({"events": events[:count]}))

    with mock.patch.object(collector_module, "MAX_EVENTS_REQUEST_BYTES", limit):
        fitting = collector_module._events_fitting_request(events)

    count = len(fitting)
    assert fitting == events[:count]
    assert bool(fitting) == bool(events)
    assert count <= 1 or body(count) <= limit
    assert count == len(events) or body(count + 1) > limit
    # Only an event that cannot fit alone is ever sent over the limit.
    if fitting and body(count) > limit:
        assert count == 1 and body(1) > limit


def test_the_bytes_measured_are_the_bytes_sent() -> None:
    """``_http_post`` sends exactly what ``_request_body`` returns, and says so
    in Content-Length, which is what the collector's limit reads."""
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = self.headers["Content-Length"]
            received.append((length, self.rfile.read(int(length))))
            self.send_response(202)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            return

    payload = {
        "events": [
            {"message": 'café \U0001f600 \x01 "quoted"', "details": {"n": 1.5}},
            {"message": None, "details": {}},
        ]
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        status, _ = collector_module._http_post(
            f"http://127.0.0.1:{server.server_port}/v1/runs/run-a/events",
            payload,
            "collector-token",
        )
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()

    body = collector_module._request_body(payload)
    assert status == 202
    assert received == [(str(len(body)), body)]
    assert json.loads(body) == payload
    assert len(body) == (
        collector_module._EVENTS_ENVELOPE_BYTES
        + sum(len(collector_module._request_body(e)) for e in payload["events"])
        + len(payload["events"])
        - 1
    )


def _oversized_event() -> dict[str, Any]:
    """An event no request can carry. Every event is bounded before this
    version queues it, but the spool is shared with other versions and holds
    whatever they queued."""

    return {**SMALL_EVENT, "details": {"blob": "x" * COLLECTOR_BODY_LIMIT}}


def test_an_event_too_large_to_send_alone_is_left_to_the_collector(
    service, collector, capsys
) -> None:
    """The events queued before it are delivered first, in a request of their
    own. It is then sent alone, and the collector's 413 settles the run."""
    service.emit(count=3)
    service.emit(_oversized_event())
    service.emit(count=2)

    service.flush_until_idle()

    assert [sequences for _, sequences in collector.event_requests] == [[1, 2, 3], [4]]
    assert collector.event_requests[1][0] > COLLECTOR_BODY_LIMIT
    assert collector.asked("events") == [202, 413]
    assert [e["sequence"] for e in collector.accepted] == [1, 2, 3]
    assert service.local_only_reason() == LOCAL_ONLY_REJECTED_EVENTS
    assert capsys.readouterr().err == _expected_warning("events", 413)
    # Local-only is final: nothing more is sent, however long the service waits.
    service.wait_out_any_retry_delay()
    assert not service.flush()
    assert len(collector.event_requests) == 2


def test_an_event_over_the_limit_that_the_collector_takes_does_not_stop_the_run(
    service, collector, capsys
) -> None:
    """The limit is the collector's to enforce: were it raised there, an event
    over this service's figure would be acknowledged like any other."""
    collector.body_limit = 4 * COLLECTOR_BODY_LIMIT
    service.emit()
    service.emit(_oversized_event())
    service.emit(count=2)

    service.flush_until_idle()

    assert [sequences for _, sequences in collector.event_requests] == [
        [1],
        [2],
        [3, 4],
    ]
    assert collector.asked("events") == [202, 202, 202]
    assert not service.spool.has_pending()
    assert service.local_only_reason() is None
    assert capsys.readouterr().err == ""


# --- A run the collector no longer has ---------------------------------------


def test_a_run_the_collector_lost_is_registered_again_and_delivered(
    service, collector, capsys
) -> None:
    service.emit(count=2)
    assert service.flush()
    service.emit(count=3)
    collector.runs.clear()  # the collector's database was restored, say

    assert not service.flush()
    # Nothing is decided yet, and nothing waits on a retry delay.
    assert service.pending() and service.local_only_reason() is None
    assert service.flush()

    assert collector.answers == [
        ("exchange", 200),
        ("register", 201),
        ("events", 202),
        ("events", 404),
        ("register", 201),
        ("events", 202),
    ]
    # The batch the collector did not find is the batch it is then sent.
    assert collector.event_requests[1][1] == collector.event_requests[2][1] == [3, 4, 5]
    assert [e["event_id"] for e in collector.accepted] == [
        e["event_id"] for e in service.queued
    ]
    assert not service.spool.has_pending()
    assert capsys.readouterr().err == ""


def test_a_run_the_collector_cannot_keep_goes_local_only_after_one_more_try(
    service, collector, capsys
) -> None:
    """A collector that answers 201 for the registration and 404 for the events
    (an older one with no such route, say) is asked twice, then left alone."""
    collector.keeps_runs = False
    service.emit()

    for _ in range(5):
        assert not service.flush()
        service.wait_out_any_retry_delay()

    assert collector.answers == [
        ("exchange", 200),
        ("register", 201),
        ("events", 404),
        ("register", 201),
        ("events", 404),
    ]
    assert service.local_only_reason() == LOCAL_ONLY_REJECTED_EVENTS
    assert capsys.readouterr().err == _expected_warning("events", 404)


def test_each_acknowledged_batch_allows_one_more_registration(
    service, collector, capsys
) -> None:
    """The collector loses the run three times, with a batch acknowledged
    between each: every loss is recovered."""
    service.emit()
    assert service.flush()
    for _ in range(3):
        service.emit()
        collector.runs.clear()
        assert not service.flush()
        assert service.flush()

    assert collector.asked("register") == [201, 201, 201, 201]
    assert collector.asked("events") == [202, 404, 202, 404, 202, 404, 202]
    assert not service.spool.has_pending()
    assert service.local_only_reason() is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [403, 409, 422])
def test_registering_again_is_answered_like_any_registration(
    service, collector, capsys, status
) -> None:
    """Another user may have registered the run in the meantime (403), or the
    owner with other metadata (409): the second registration settles the run
    as the first would have."""
    service.emit()
    assert service.flush()
    service.emit()
    collector.runs.clear()
    assert not service.flush()
    collector.refuse["register"] = status

    for _ in range(3):
        assert not service.flush()
        service.wait_out_any_retry_delay()

    assert collector.asked("register") == [201, status]
    assert collector.asked("events") == [202, 404]
    assert service.local_only_reason() == LOCAL_ONLY_REJECTED_REGISTRATION
    assert capsys.readouterr().err == _expected_warning("register", status)


@pytest.mark.parametrize("status", [503, 429, 408])
def test_registering_again_waits_out_a_transient_answer(
    service, collector, capsys, status
) -> None:
    service.emit()
    assert service.flush()
    service.emit()
    collector.runs.clear()
    assert not service.flush()
    collector.refuse["register"] = status

    assert not service.flush()
    assert not service.flush()  # inside the retry delay: nothing is sent
    assert collector.asked("register") == [201, status]
    del collector.refuse["register"]
    service.wait_out_any_retry_delay()
    assert service.flush()

    assert collector.asked("register") == [201, status, 201]
    assert collector.asked("events") == [202, 404, 202]
    assert not service.spool.has_pending()
    assert capsys.readouterr().err == ""


def test_no_other_answer_to_events_registers_the_run_again(spool) -> None:
    """Over every status but 404: a run is registered once."""
    for status in [s for s in range(200, 600) if s not in (202, 404)]:
        collector = FakeCollector()
        collector.refuse["events"] = status
        with _service(spool, collector) as service:
            service.emit()
            for _ in range(4):
                assert not service.flush(), status
                service.wait_out_any_retry_delay()
            assert collector.asked("register") == [201], status


def _assert_registrations_are_bounded(collector: FakeCollector) -> None:
    """Between two registrations the collector accepted there is a 404 for the
    run's events; from the second on, an acknowledged batch as well."""

    since: list[int] = []
    accepted_registrations = 0
    for kind, status in collector.answers:
        if kind == "events":
            since.append(status)
        elif kind == "register" and status == 201:
            if accepted_registrations >= 1:
                assert 404 in since, collector.answers
            if accepted_registrations >= 2:
                assert 202 in since, collector.answers
            accepted_registrations += 1
            since = []


STEPS = st.lists(
    st.sampled_from(
        ["emit", "flush", "flush", "flush", "lose", "stop_keeping", "keep_again"]
    ),
    max_size=40,
)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=STEPS)
@example(steps=["emit", "flush", "emit", "lose", "flush", "lose", "flush", "flush"])
@example(steps=["stop_keeping", "emit", "flush", "flush", "keep_again", "flush"])
def test_a_lost_run_costs_a_bounded_number_of_registrations(spool, steps) -> None:
    """The collector loses the run, or stops keeping registrations, at any
    point. After every step: registrations stay bounded as above; a run goes
    local-only only on a second 404 with nothing acknowledged since the first;
    nothing is sent after that; and what the collector acknowledged is the
    queue in order, none skipped or repeated. In the end, a collector that
    keeps the run again receives everything, unless the run went local-only."""
    collector = FakeCollector()
    with _service(spool, collector) as service:
        settled_at: int | None = None

        def check() -> None:
            nonlocal settled_at
            _assert_registrations_are_bounded(collector)
            accepted = [event["event_id"] for event in collector.accepted]
            queued = [event["event_id"] for event in service.queued]
            assert accepted == queued[: len(accepted)]
            reason = service.local_only_reason()
            if settled_at is not None:
                assert len(collector.answers) == settled_at, "sent after local-only"
            elif reason is not None:
                assert reason == LOCAL_ONLY_REJECTED_EVENTS
                events = collector.asked("events")
                assert events[-1] == 404
                # The 404 before it, with only the registration in between.
                assert collector.answers[-3:] == [
                    ("events", 404),
                    ("register", 201),
                    ("events", 404),
                ]
                settled_at = len(collector.answers)

        for step in steps:
            if step == "emit":
                service.emit()
            elif step == "flush":
                service.flush()
            elif step == "lose":
                collector.runs.clear()
            else:
                collector.keeps_runs = step == "keep_again"
            check()

        collector.keeps_runs = True
        for _ in range(4):
            service.flush()
            check()
        if settled_at is None:
            assert not service.spool.run_has_deliverable(
                service.registration["run_id"], service.registration["producer_id"]
            )
            assert len(collector.accepted) == len(service.queued)

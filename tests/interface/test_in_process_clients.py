"""The emitter's in-process clients share the interface's definitions.

`microcosm_emitter.telemetry.client` and `microcosm_emitter.orrery.client`
build their messages, run identity, receipts and value conversion with the
interface's code, so the two kinds of client send the same messages and handle
unconvertible arguments the same way: they report failure, never raise.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import microcosm_emitter_interface.orrery as interface_orrery
import microcosm_emitter_interface.serialization as interface_serialization
import microcosm_emitter_interface.telemetry as interface_telemetry
from microcosm_emitter_interface.protocol_fixtures import materialize, protocol_fixtures


class MultiElementArray:
    def item(self):
        error_message = "can only convert an array of size 1 to a Python scalar"
        raise ValueError(error_message)


class UnresolvablePath:
    def __fspath__(self):
        error_message = "synthetic unresolvable path"
        raise OSError(error_message)

    def resolve(self):
        error_message = "synthetic unresolvable path"
        raise OSError(error_message)


def test_in_process_clients_reuse_the_interface_definitions():
    from microcosm_emitter.orrery import client as orrery_client
    from microcosm_emitter.orrery import contracts as orrery_contracts
    from microcosm_emitter.telemetry import client as telemetry_client
    from microcosm_emitter.telemetry import serialization

    assert serialization.wire_value is interface_serialization.wire_value
    assert telemetry_client.TelemetryRun is interface_telemetry.TelemetryRun
    for name in (
        "PublicationFileRole",
        "PublicationFile",
        "PublicationInventory",
        "PublicationReceipt",
    ):
        assert getattr(orrery_contracts, name) is getattr(interface_orrery, name)
    from microcosm_emitter_interface import default_spool_path

    assert orrery_client.default_spool_path is default_spool_path


class RecordingTelemetryTransport:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def send(self, message):
        self.requests.append(dict(message))

    def close(self):
        self.closed = True


def in_process_telemetry():
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    transport = RecordingTelemetryTransport()
    emitter = LocalTelemetryEmitter(
        run=TelemetryRun("fixture-run", "UK", "fixture-pipeline"),
        transport=transport,
        close_service_on_finish=False,
    )
    return emitter, transport


#: The in-process call equivalent to each telemetry fixture's typed call.
IN_PROCESS_SENDS: dict[str, Callable[[Any], None]] = {
    "telemetry-emit": lambda emitter: emitter.emit(
        event_type="progress",
        status="progress",
        stage_id="compile",
        message="Compiling.",
        details={"step": 1},
    ),
    "telemetry-stage": lambda emitter: emitter.stage("compile", rows=3),
    "telemetry-transition-stage": lambda emitter: emitter.transition_stage(
        "calibrating", message="Calibrating."
    ),
    "telemetry-progress": lambda emitter: emitter.progress(
        "calibrating", done=2, total=10, unit="epochs", phase="solve"
    ),
    "telemetry-calibration-progress": lambda emitter: emitter.calibration_progress(
        {"kind": "calibration_epoch", "epoch": 10, "epochs": 100, "loss": 0.25}
    ),
    "telemetry-transition-calibration-progress": lambda emitter: (
        emitter.transition_calibration_progress(
            {"kind": "calibration_epoch", "epoch": 20, "epochs": 100, "loss": 0.125}
        )
    ),
    "telemetry-fail": lambda emitter: emitter.fail(
        RuntimeError("Synthetic failure."),
        failed_during="calibrating",
        error_code="BUILD_FAILED",
    ),
    "telemetry-fail-without-code": lambda emitter: emitter.fail(
        MemoryError("Synthetic failure."), failure_class="oom"
    ),
    "telemetry-refusal": lambda emitter: emitter.block(
        phase="preflight",
        blocking_gate_ids=["fixture-check"],
        blocking_failure_count=1,
        gate_statuses={"fixture-check": "failed"},
    ),
    "telemetry-complete": lambda emitter: emitter.complete(),
}


def test_every_telemetry_message_fixture_has_an_in_process_call():
    assert set(IN_PROCESS_SENDS) == {
        fixture.name
        for fixture in protocol_fixtures()
        if fixture.module == "telemetry" and fixture.kind == "message"
    }


@pytest.mark.parametrize("name", sorted(IN_PROCESS_SENDS))
def test_in_process_telemetry_sends_the_recorded_message(tmp_path, name):
    (fixture,) = [fixture for fixture in protocol_fixtures() if fixture.name == name]
    emitter, transport = in_process_telemetry()
    IN_PROCESS_SENDS[name](emitter)
    assert transport.requests == [
        materialize(fixture, directory=tmp_path, spool_path=tmp_path)
    ]


def test_in_process_telemetry_reports_unconvertible_arguments(capsys):
    emitter, transport = in_process_telemetry()
    emitter.stage("compile", value=MultiElementArray())
    emitter.block(phase="preflight", blocking_gate_ids=MultiElementArray())
    assert transport.requests == []
    assert capsys.readouterr().err.count("could not be queued") == 1


class RecordingGraphTransport:
    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]]):
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def request(self, message, *, timeout=None):
        self.requests.append(dict(message))
        return self.answer(dict(message))

    def close(self):
        pass


def test_in_process_orrery_client_reports_unconvertible_arguments():
    from microcosm_emitter.orrery.client import GraphPublicationClient

    transport = RecordingGraphTransport(lambda message: {"receipt": None})
    client = GraphPublicationClient(transport)
    assert client.inventory(UnresolvablePath(), {"graph.json": "graph"}) is None
    receipt = client.enqueue(
        UnresolvablePath(),
        {"version": 1, "publication_id": "fixture-publication", "files": []},
    )
    assert receipt["status"] == "failed"
    assert receipt["publication_id"] == "fixture-publication"
    assert receipt["error_code"] == "invalid_arguments"
    missing_identifier = client.enqueue(Path("/tmp"), {"version": 1, "files": []})
    assert missing_identifier["status"] == "failed"
    assert missing_identifier["publication_id"] is None
    assert transport.requests == []


def test_in_process_orrery_client_reports_an_unexpected_transport_error():
    from microcosm_emitter.orrery.client import GraphPublicationClient

    def unexpected(message):
        error_message = "synthetic"
        raise RecursionError(error_message)

    client = GraphPublicationClient(RecordingGraphTransport(unexpected))
    receipt = client.receipt("fixture-publication")
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "local_service_unavailable"


def test_in_process_orrery_client_sends_the_recorded_queries(tmp_path):
    from microcosm_emitter.orrery.client import GraphPublicationClient

    fixtures = {
        fixture.name: fixture
        for fixture in protocol_fixtures()
        if fixture.module == "orrery" and fixture.kind == "query"
    }
    directory = tmp_path / "inputs"
    inventory = materialize(
        fixtures["orrery-enqueue"], directory=directory, spool_path=tmp_path
    )["inventory"]
    transport = RecordingGraphTransport(
        lambda message: {
            "receipt": {
                "version": 1,
                "publication_id": "fixture-publication",
                "status": "pending",
                "error_code": None,
                "attempts": 1,
            }
        }
    )
    client = GraphPublicationClient(transport)
    client.inventory(
        directory,
        {"graph.orrery.json": "graph", "graph.schema.json": "schema"},
        publication_id="fixture-publication",
    )
    client.enqueue(directory, inventory)
    client.receipt("fixture-publication")
    client.retry("fixture-publication")
    expected = [
        materialize(fixtures[name], directory=directory.resolve(), spool_path=tmp_path)
        for name in (
            "orrery-inventory",
            "orrery-enqueue",
            "orrery-receipt",
            "orrery-retry",
        )
    ]
    assert transport.requests == expected

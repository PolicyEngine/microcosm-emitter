"""Client lifecycle works through a fake transport, without starting a service."""

import pytest
from microcosm_provider_telemetry import client
from microcosm_provider_telemetry.client import LocalTelemetryEmitter, TelemetryRun


class RecordingTransport:
    def __init__(self):
        self.events = []
        self.closed = False

    def send(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


@pytest.fixture
def emitter():
    transport = RecordingTransport()
    return LocalTelemetryEmitter(
        run=TelemetryRun("run", "UK", "test"), transport=transport
    )


def test_stage_transition_and_completion(emitter):
    emitter.transition_stage("load")
    emitter.transition_stage("compile")
    emitter.transition_stage("compile")
    emitter.complete()
    assert [
        (event["stage_id"], event["status"]) for event in emitter._transport.events
    ] == [
        ("load", "started"),
        ("load", "completed"),
        ("compile", "started"),
        ("compile", "progress"),
        ("compile", "completed"),
        ("complete", "completed"),
    ]
    assert not emitter.available
    assert emitter._transport.closed


def test_failure_closes_stage_then_run_and_redacts(emitter):
    emitter.transition_stage("compile")
    emitter.fail(ValueError("Bearer hf_abcdefghijk"))
    events = emitter._transport.events
    assert [(event["event_type"], event["status"]) for event in events] == [
        ("stage", "started"),
        ("stage", "failed"),
        ("run", "failed"),
    ]
    assert events[-1]["details"]["failed_during"] == "compile"
    assert "hf_abcdefghijk" not in events[-1]["message"]


def test_close_does_not_report_success_and_is_idempotent(emitter):
    emitter.close()
    emitter.close()
    emitter.stage("ignored")
    assert emitter._transport.events == []


def test_startup_failure_is_disabled_without_leaking_exception_details(
    monkeypatch, capsys, tmp_path
):
    def fail(*args, **kwargs):
        raise PermissionError("sensitive detail")

    monkeypatch.setattr(client, "start_service", fail)
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "spool"
    )
    emitter.stage("safe")
    emitter.complete()
    assert not emitter.available
    warning = capsys.readouterr().err
    assert "PermissionError" in warning
    assert "sensitive detail" not in warning


def test_send_and_close_failures_never_interrupt_build(emitter, monkeypatch, capsys):
    def fail(*args):
        raise OSError("private")

    monkeypatch.setattr(emitter._transport, "send", fail)
    monkeypatch.setattr(emitter._transport, "close", fail)
    emitter.stage("one")
    emitter.stage("two")
    emitter.close()
    assert capsys.readouterr().err.count("could not be queued") == 1
    assert not emitter.available


def test_calibration_and_batch_progress(emitter):
    emitter.transition_calibration_progress({"kind": "other"})
    emitter.transition_calibration_progress({"kind": "calibration_epoch", "epoch": 1})
    emitter.progress("compile", done=2, total=4, unit="batches")
    assert [event["event_type"] for event in emitter._transport.events] == [
        "stage",
        "calibration",
        "progress",
    ]
    assert emitter._transport.events[-1]["details"]["done"] == 2

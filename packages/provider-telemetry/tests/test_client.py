"""The adapter forwards requests without interpreting telemetry state or details."""

from pathlib import Path

import pytest
from microcosm_provider_telemetry import client
from microcosm_provider_telemetry.client import LocalTelemetryEmitter, TelemetryRun


class RecordingTransport:
    def __init__(self):
        self.requests = []
        self.closed = False

    def send(self, request):
        self.requests.append(request)

    def close(self):
        self.closed = True


@pytest.fixture
def emitter():
    return LocalTelemetryEmitter(
        run=TelemetryRun("run", "UK", "test"), transport=RecordingTransport()
    )


def test_stage_requests_do_not_infer_completion_or_progress(emitter):
    emitter.transition_stage("load")
    emitter.transition_stage("compile")
    emitter.transition_stage("compile")
    emitter.complete()
    assert [request["command"] for request in emitter._transport.requests] == [
        "transition_stage",
        "transition_stage",
        "transition_stage",
        "complete",
    ]
    assert all("event_type" not in request for request in emitter._transport.requests)
    assert not hasattr(emitter, "_transition_stage")
    assert emitter._transport.closed and not emitter.available


def test_failure_serializes_exception_and_leaves_stage_selection_to_service(emitter):
    emitter.transition_stage("compile")
    emitter.fail(ValueError("Bearer hf_abcdefghijk"))
    request = emitter._transport.requests[-1]
    assert request == {
        "command": "fail",
        "message": "Bearer hf_abcdefghijk",
        "error_type": "ValueError",
        "failed_during": None,
        "failure_class": "build_failure",
    }
    assert emitter._transport.closed


def test_adapter_does_not_filter_calibration_or_redact_details(emitter):
    emitter.transition_calibration_progress({"kind": "other"})
    emitter.transition_calibration_progress({"kind": "calibration_epoch", "epoch": 1})
    emitter.progress("compile", done=2, total=4, HF_TOKEN="hf_private_test")
    requests = emitter._transport.requests
    assert [request["command"] for request in requests] == [
        "transition_calibration_progress",
        "transition_calibration_progress",
        "progress",
    ]
    assert requests[-1]["details"]["HF_TOKEN"] == "hf_private_test"
    assert requests[-1]["done"] == 2


def test_close_does_not_report_an_outcome_and_is_idempotent(emitter):
    emitter.close()
    emitter.close()
    emitter.stage("ignored")
    assert emitter._transport.requests == []


def test_startup_passes_identity_to_service_without_creating_events(
    monkeypatch, tmp_path
):
    transport = RecordingTransport()
    from unittest.mock import Mock

    handle = Mock(client=transport)
    launch = Mock(return_value=handle)
    monkeypatch.setattr(client, "start_service", launch)
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"),
        spool_path=tmp_path / "spool",
        identity={"HF_TOKEN": "hf_private_test"},
    )
    try:
        assert launch.call_args.args[1]["identity"] == {"HF_TOKEN": "hf_private_test"}
        assert transport.requests == []
    finally:
        emitter.close()


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
    assert "PermissionError" in warning and "sensitive detail" not in warning


def test_send_and_close_failures_never_interrupt_build(emitter, monkeypatch, capsys):
    def fail(*args):
        raise OSError("private")

    monkeypatch.setattr(emitter._transport, "send", fail)
    monkeypatch.setattr(emitter._transport, "close", fail)
    emitter.stage("one")
    emitter.stage("two")
    emitter.complete()
    assert capsys.readouterr().err.count("could not be queued") == 1
    assert not emitter.available


def test_failure_with_broken_exception_string_still_closes(emitter):
    class BrokenError(Exception):
        def __str__(self):
            raise RuntimeError("broken exception formatting")

    emitter.fail(BrokenError())
    assert emitter._transport.requests[-1]["command"] == "fail"
    assert emitter._transport.requests[-1]["error_type"] == "BrokenError"
    assert emitter._transport.requests[-1]["message"] is None
    assert emitter._transport.closed and not emitter.available


def test_serialization_preserves_paths_and_handles_nonfinite_numbers(emitter):
    emitter.stage("compile", path=Path("/tmp/test"), loss=float("nan"))
    assert emitter._transport.requests[-1]["details"] == {
        "path": "/tmp/test",
        "loss": None,
    }

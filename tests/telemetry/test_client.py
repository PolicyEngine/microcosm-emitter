"""The adapter forwards requests without interpreting telemetry state or details."""

from pathlib import Path

import pytest

from microcosm_emitter.telemetry import client
from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun


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
        error_message = "sensitive detail"
        raise PermissionError(error_message)

    monkeypatch.setattr(client, "start_service", fail)
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "spool"
    )
    emitter.stage("safe")
    emitter.complete()
    assert not emitter.available
    warning = capsys.readouterr().err
    assert "startup_failure" in warning
    assert "PermissionError" not in warning and "sensitive detail" not in warning


def test_send_and_close_failures_never_interrupt_build(emitter, monkeypatch, capsys):
    def fail(*args):
        error_message = "private"
        raise OSError(error_message)

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
            error_message = "broken exception formatting"
            raise RuntimeError(error_message)

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


def test_block_forwards_metadata_without_constructing_outcome(emitter):
    emitter.block(phase="preflight", blocking_gate_ids=("uk_local_target_fit",))
    assert emitter._transport.requests == [
        {
            "command": "block",
            "phase": "preflight",
            "blocking_gate_ids": ["uk_local_target_fit"],
            "blocking_failure_count": None,
            "gate_statuses": None,
        }
    ]
    assert emitter._transport.closed and not emitter.available


def test_failure_code_is_forwarded_without_classifying_exception(emitter):
    emitter.fail(
        KeyboardInterrupt("operator"),
        failed_during="compile",
        failure_class="interrupted",
        error_code="BUILD_INTERRUPTED",
    )
    assert emitter._transport.requests[-1]["error_code"] == "BUILD_INTERRUPTED"
    assert emitter._transport.requests[-1]["failure_class"] == "interrupted"
    assert emitter._transport.requests[-1]["error_type"] == "KeyboardInterrupt"
    assert emitter._transport.closed


def test_block_transport_failure_still_closes(emitter, monkeypatch):
    def failed(request):
        error_message = "synthetic private error"
        raise OSError(error_message)

    monkeypatch.setattr(emitter._transport, "send", failed)
    emitter.block(phase="preflight", blocking_gate_ids=["check"])
    assert emitter._transport.closed and not emitter.available


@pytest.mark.parametrize("outcome", ["complete", "fail", "block"])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("send_fails", [False, True])
def test_outcome_cleanup_can_be_deferred(outcome, deferred, send_fails, monkeypatch):
    transport = RecordingTransport()
    emitter = LocalTelemetryEmitter(
        run=TelemetryRun("run", "UK", "test"),
        transport=transport,
        close_service_on_finish=not deferred,
    )
    if send_fails:

        def failed(request):
            error_message = "synthetic send failure"
            raise OSError(error_message)

        monkeypatch.setattr(transport, "send", failed)
    try:
        if outcome == "fail":
            emitter.fail(RuntimeError("synthetic"), error_code="BUILD_FAILED")
        elif outcome == "block":
            emitter.block(phase="preflight", blocking_gate_ids=["check"])
        else:
            emitter.complete()
        assert transport.closed is not deferred
        assert emitter.available is deferred
    finally:
        emitter.close()
    assert transport.closed and not emitter.available


@pytest.mark.parametrize("outcome", ["complete", "fail", "block"])
def test_cleanup_failure_is_contained_for_every_outcome(outcome, monkeypatch):
    transport = RecordingTransport()
    emitter = LocalTelemetryEmitter(
        run=TelemetryRun("run", "UK", "test"),
        transport=transport,
    )

    def failed():
        error_message = "synthetic cleanup failure"
        raise OSError(error_message)

    monkeypatch.setattr(transport, "close", failed)
    if outcome == "fail":
        emitter.fail(RuntimeError("synthetic"))
    elif outcome == "block":
        emitter.block(phase="preflight", blocking_gate_ids=["check"])
    else:
        emitter.complete()
    assert not emitter.available


def test_start_propagates_deferred_cleanup(monkeypatch, tmp_path):
    from unittest.mock import Mock

    transport = RecordingTransport()
    handle = Mock(client=transport)
    monkeypatch.setattr(client, "start_service", Mock(return_value=handle))
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"),
        spool_path=tmp_path / "spool",
        close_service_on_finish=False,
    )
    try:
        emitter.complete()
        handle.close.assert_not_called()
        assert emitter.available
    finally:
        emitter.close()
    handle.close.assert_called_once()


@pytest.mark.parametrize("timeout", [None, 0.75])
def test_telemetry_start_uses_shared_default_and_preserves_override(
    monkeypatch, tmp_path, timeout
):
    from unittest.mock import Mock

    from microcosm_emitter.host.constants import STARTUP_TIMEOUT

    launch = Mock(return_value=Mock(client=RecordingTransport()))
    monkeypatch.setattr(client, "start_service", launch)
    kwargs = {} if timeout is None else {"startup_timeout_seconds": timeout}
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "jobs", **kwargs
    )
    assert emitter.available
    assert STARTUP_TIMEOUT == 15.0
    assert launch.call_args.kwargs["startup_timeout"] == (
        STARTUP_TIMEOUT if timeout is None else timeout
    )
    assert emitter.startup_error is None
    emitter.close()


def test_telemetry_warning_and_retained_error_use_safe_startup_reason(
    monkeypatch, tmp_path, capsys
):
    from unittest.mock import Mock

    from microcosm_emitter.host import errors

    failure = errors.ServiceStartupError("readiness_timeout")
    monkeypatch.setattr(client, "start_service", Mock(side_effect=failure))
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "jobs"
    )
    assert not emitter.available
    assert emitter.startup_error.reason == "readiness_timeout"
    assert "readiness_timeout" in capsys.readouterr().err


def test_telemetry_diagnostic_does_not_retain_raw_exception(
    monkeypatch, tmp_path, capsys
):
    from unittest.mock import Mock

    monkeypatch.setattr(
        client,
        "start_service",
        Mock(side_effect=PermissionError("Bearer hf_synthetic_private_payload")),
    )
    emitter = LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "jobs"
    )
    assert emitter.startup_error.reason == "startup_failure"
    assert emitter.startup_error.exit_code is None
    assert str(emitter.startup_error) == "Service startup failed (startup_failure)."
    assert not hasattr(emitter.startup_error, "error_type")
    assert emitter.startup_error.__cause__ is None
    assert emitter.startup_error.__context__ is None
    assert "hf_synthetic_private_payload" not in capsys.readouterr().err

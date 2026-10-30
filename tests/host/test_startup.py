"""Startup shares one deadline and reports only safe failure metadata."""

import json
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from microcosm_emitter.host import launcher
from microcosm_emitter.host.constants import STARTUP_TIMEOUT
from microcosm_emitter.host.errors import LocalServiceError


@pytest.fixture
def startup(tmp_path, monkeypatch):
    state = SimpleNamespace(now=0.0, ready_at=0.0, ping_delay=0.0, transfer_delay=0.0)
    directory = tmp_path / "runtime"
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0
    launch = Mock(return_value=process)
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=False)
    writer.sendall.side_effect = lambda payload: advance(state.transfer_delay)
    reader = Mock()
    reader.__enter__ = Mock(return_value=reader)
    reader.__exit__ = Mock(return_value=False)
    timeouts = []

    def advance(seconds):
        state.now += seconds

    class Client:
        timeout = 0.2

        def __init__(self, path):
            self.socket_path = path

        def ping(self, *, timeout=None):
            timeouts.append(self.timeout if timeout is None else timeout)
            advance(state.ping_delay)
            if state.now < state.ready_at:
                error_message = "not ready"
                raise LocalServiceError(error_message)

    def create_directory(**kwargs):
        directory.mkdir()
        return str(directory)

    monkeypatch.setattr(
        launcher, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=advance)
    )
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", create_directory)
    monkeypatch.setattr(
        launcher.psutil,
        "Process",
        lambda pid: SimpleNamespace(pid=pid, create_time=lambda: 1.0),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    monkeypatch.setattr(launcher.socket, "socketpair", lambda: (reader, writer))
    monkeypatch.setattr(launcher, "SocketClient", Client)
    return SimpleNamespace(
        state=state,
        process=process,
        launch=launch,
        directory=directory,
        timeouts=timeouts,
        writer=writer,
    )


def test_shared_default_accepts_readiness_after_old_three_second_limit(startup):
    assert STARTUP_TIMEOUT == 15.0  # User-approved shared startup allowance.
    startup.state.ready_at = 3.5
    handle = launcher.start_service("test", {})
    assert 3.5 <= startup.state.now < STARTUP_TIMEOUT
    assert handle.process is startup.process
    startup.process.terminate.assert_not_called()


def test_success_after_deadline_is_not_accepted(startup):
    startup.state.ping_delay = 0.06
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "readiness_timeout"
    assert startup.timeouts == [0.05]
    startup.process.terminate.assert_called_once()
    assert not startup.directory.exists()


def test_explicit_short_deadline_bounds_poll_sleep_and_reaps_child(startup):
    startup.state.ready_at = 1.0
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.03)
    assert captured.value.reason == "readiness_timeout"
    assert startup.state.now == pytest.approx(0.03)
    assert startup.timeouts == pytest.approx([0.03, 0.01])
    startup.process.terminate.assert_called_once()
    startup.process.wait.assert_called_once()
    assert not startup.directory.exists()


def test_configuration_transfer_uses_the_same_deadline(startup):
    startup.state.transfer_delay = 0.04
    startup.state.ready_at = 1.0
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "readiness_timeout"
    assert startup.state.now == pytest.approx(0.05)
    assert startup.timeouts == pytest.approx([0.01])
    assert not startup.directory.exists()


def test_configuration_transfer_that_consumes_deadline_never_pings(startup):
    startup.state.transfer_delay = 0.06
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "configuration_timeout"
    assert startup.timeouts == []
    assert not startup.directory.exists()


def test_child_exit_preserves_safe_exit_code_and_cleans_directory(startup):
    startup.process.poll.return_value = 7
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {})
    assert captured.value.reason == "child_exit"
    assert captured.value.exit_code == 7
    assert "7" in str(captured.value)
    startup.process.wait.assert_called_once()
    assert not startup.directory.exists()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), "private"])
def test_invalid_deadline_starts_no_child(startup, timeout):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=timeout)
    assert captured.value.reason == "invalid_configuration"
    startup.launch.assert_not_called()
    assert not startup.directory.exists()


def test_invalid_configuration_starts_no_child_and_has_safe_reason(startup):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_services({"module": {"token": object()}})
    assert captured.value.reason == "invalid_configuration"
    startup.launch.assert_not_called()


def test_startup_error_and_traceback_do_not_retain_raw_exception(startup):
    startup.launch.side_effect = PermissionError("Bearer hf_synthetic_private_payload")
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {"token": "hf_synthetic_configuration"})
    assert captured.value.reason == "startup_failure"
    assert str(captured.value) == "Service startup failed (startup_failure)."
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered = "".join(traceback.format_exception(captured.value))
    assert "hf_synthetic_private_payload" not in rendered
    assert "hf_synthetic_configuration" not in str(captured.value)
    assert not startup.directory.exists()


def test_custom_exception_type_name_is_not_a_startup_diagnostic(startup):
    private_error = type(
        "hf_synthetic_private_class", (Exception,), {"__module__": "builtins"}
    )
    startup.launch.side_effect = private_error("private payload")
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {})
    assert captured.value.reason == "startup_failure"
    assert str(captured.value) == "Service startup failed (startup_failure)."
    assert "hf_synthetic_private_class" not in str(captured.value)
    assert "private payload" not in "".join(traceback.format_exception(captured.value))


def test_zero_deadline_never_transfers_configuration_and_still_reaps(startup):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0)
    assert captured.value.reason == "configuration_timeout"
    startup.writer.sendall.assert_not_called()
    startup.process.terminate.assert_called_once()
    assert not startup.directory.exists()


def test_readiness_probe_has_one_deadline_across_partial_reads(monkeypatch, tmp_path):
    from microcosm_emitter.host import client

    clock = SimpleNamespace(now=0.0)
    timeouts = []
    peer = Mock()
    peer.__enter__ = Mock(return_value=peer)
    peer.__exit__ = Mock(return_value=False)
    peer.settimeout.side_effect = timeouts.append

    def read(size):
        clock.now += 0.02
        return b"o"  # Never completes an acknowledgement.

    peer.recv.side_effect = read
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(client.socket, "socket", lambda *args: peer)
    with pytest.raises(LocalServiceError, match="TimeoutError"):
        client.SocketClient(tmp_path / "socket").ping(timeout=0.03)
    assert timeouts == pytest.approx([0.03, 0.03, 0.03, 0.01])
    assert peer.recv.call_count == 2


def test_unknown_reason_cannot_be_used_as_private_diagnostic():
    from microcosm_emitter.host.errors import ServiceStartupError

    with pytest.raises(ValueError, match="Unknown startup failure reason") as captured:
        ServiceStartupError("hf_synthetic_private_reason")
    assert "hf_synthetic_private_reason" not in str(captured.value)


def test_child_startup_stderr_redacts_exception_payload_and_spoofed_type(
    monkeypatch, tmp_path, capsys
):
    import io

    from microcosm_emitter.host import __main__ as entry

    directory = tmp_path / "runtime"
    directory.mkdir()
    monkeypatch.setattr(
        entry.sys,
        "argv",
        [
            "service",
            "--socket",
            str(directory / "socket"),
            "--parent-pid",
            "123",
            "--parent-created-at",
            "1",
        ],
    )
    monkeypatch.setattr(
        entry.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b'{"test":{"token":"hf_synthetic_private_configuration"}}'
            )
        ),
    )
    private_error = type(
        "hf_synthetic_private_class", (Exception,), {"__module__": "builtins"}
    )
    monkeypatch.setattr(
        entry,
        "load_module",
        Mock(side_effect=private_error("Bearer hf_synthetic_private_payload")),
    )
    assert entry.main() == 1
    warning = capsys.readouterr().err
    assert "startup_failure" in warning
    assert warning == "Service startup failed (startup_failure).\n"
    assert "hf_synthetic_private" not in warning
    assert not directory.exists()


@pytest.mark.parametrize(
    "settings", [[], "settings", None], ids=["list", "text", "null"]
)
def test_module_settings_that_are_not_an_object_fail_with_the_fixed_reason(
    monkeypatch, tmp_path, capsys, settings
):
    import io

    from microcosm_emitter.host import __main__ as entry

    directory = tmp_path / "runtime"
    directory.mkdir()
    payload = json.dumps({"telemetry": settings}).encode()
    monkeypatch.setattr(entry.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    load_module = Mock()
    monkeypatch.setattr(entry, "load_module", load_module)

    exit_code = entry.main(
        [
            "--socket",
            str(directory / "socket"),
            "--parent-pid",
            "123",
            "--parent-created-at",
            "1",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == "Service startup failed (startup_failure).\n"
    load_module.assert_not_called()
    assert not directory.exists()


@pytest.mark.parametrize("custom", [False, True])
def test_startup_diagnostic_omits_all_exception_names_and_payloads(custom):
    from microcosm_emitter.host.errors import safe_startup_error

    error_class = (
        type("hf_synthetic_private_class", (Exception,), {})
        if custom
        else PermissionError
    )
    failure = safe_startup_error(error_class("Bearer hf_synthetic_private_payload"))
    assert str(failure) == "Service startup failed (startup_failure)."
    assert failure.reason == "startup_failure" and failure.exit_code is None
    assert not hasattr(failure, "error_type")
    assert failure.__cause__ is failure.__context__ is failure.__traceback__ is None


def test_startup_diagnostic_clones_only_reason_and_exit_code():
    from microcosm_emitter.host.errors import ServiceStartupError, safe_startup_error

    def fail_with_a_private_cause() -> None:
        startup_reason = "child_exit"
        raise ServiceStartupError(startup_reason, exit_code=-9) from PermissionError(
            "Bearer hf_synthetic_private_payload"
        )

    try:
        fail_with_a_private_cause()
    except ServiceStartupError as original:
        failure = safe_startup_error(original)
        assert failure is not original
        assert (
            str(failure)
            == "Service exited before readiness (child_exit, exit_code=-9)."
        )
        assert failure.reason == "child_exit" and failure.exit_code == -9
        assert not hasattr(failure, "error_type")
        assert failure.__cause__ is failure.__context__ is failure.__traceback__ is None

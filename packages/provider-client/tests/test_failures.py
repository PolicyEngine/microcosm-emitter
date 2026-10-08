"""Lifecycle failures cannot leave an unowned child or an open socket."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from microcosm_provider_client import launcher
from microcosm_provider_client.errors import LocalServiceError
from microcosm_provider_client.process import ParentProcess
from microcosm_provider_client.runtime import ServiceRuntime


def test_partial_initialization_is_closed(tmp_path):
    module = Mock()
    module.initialize.side_effect = RuntimeError("partial initialization")
    runtime = ServiceRuntime(tmp_path / "socket", module, is_parent_alive=lambda: True)
    with pytest.raises(RuntimeError, match="partial initialization"):
        runtime.run()
    module.close.assert_called_once()
    assert not runtime.socket_path.exists()


def test_startup_failure_reaps_child_even_when_directory_already_removed(
    tmp_path, monkeypatch
):
    directory = tmp_path / "runtime"
    directory.mkdir()
    process = Mock()

    def exited():
        directory.rmdir()
        raise LocalServiceError("not ready")

    process.poll.side_effect = [None, 1, 1]
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        launcher.SocketClient,
        "ping",
        lambda self: exited(),
    )
    with pytest.raises(LocalServiceError, match="exited"):
        launcher.start_service("missing", {})
    process.wait.assert_called_once()
    assert not directory.exists()


def test_startup_timeout_kills_child_that_ignores_termination(tmp_path, monkeypatch):
    directory = tmp_path / "runtime"
    directory.mkdir()
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("service", 1), 0]
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(LocalServiceError, match="timed out"):
        launcher.start_service("missing", {}, startup_timeout=0)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    assert not directory.exists()


@pytest.mark.parametrize(
    "created,status,alive",
    [
        (11, psutil.STATUS_RUNNING, True),
        (10, psutil.STATUS_ZOMBIE, True),
        (10, psutil.STATUS_RUNNING, False),
    ],
)
def test_parent_identity_rejects_reused_pid_zombie_or_exit(
    monkeypatch, created, status, alive
):
    process = SimpleNamespace(
        create_time=lambda: created, status=lambda: status, is_running=lambda: alive
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    assert not ParentProcess(123, 10).alive()


def test_absent_module_fails_cleanly_in_real_child():
    with pytest.raises(LocalServiceError, match="exited"):
        launcher.start_service("not-installed", {})

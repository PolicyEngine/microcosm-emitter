"""The service host works without importing any telemetry implementation."""

import socket
import tempfile
import threading
import time
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from policyengine_local_service.client import SocketClient
from policyengine_local_service.errors import LocalServiceError
from policyengine_local_service.loading import load_module
from policyengine_local_service.runtime import ServiceRuntime


class RecordingModule:
    def __init__(self):
        self.messages = []
        self.closed = threading.Event()
        self.exited = False
        self.fail_tick = False

    def initialize(self):
        pass

    def handle_message(self, message):
        if message.get("reject"):
            raise ValueError("rejected")
        self.messages.append(dict(message))

    def tick(self, now):
        if self.fail_tick:
            raise RuntimeError("periodic failure")

    def parent_exited(self):
        self.exited = True

    def close(self, deadline):
        self.closed.set()


@pytest.fixture
def socket_path():
    with tempfile.TemporaryDirectory(prefix="pe-test-", dir="/tmp") as directory:
        yield Path(directory) / "socket"


@pytest.fixture
def running(socket_path):
    module = RecordingModule()
    path = socket_path
    runtime = ServiceRuntime(
        path, module, is_parent_alive=lambda: True, tick_seconds=0.01
    )
    errors = []

    def serve():
        try:
            runtime.run()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    try:
        yield module, SocketClient(path), errors
    finally:
        runtime.stop()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not path.exists()


def test_messages_acknowledge_after_handling_and_close(running):
    module, client, errors = running
    client.send({"number": 4})
    assert module.messages == [{"number": 4}]
    client.close()
    client.close()
    assert module.closed.wait(2)
    assert not errors


def test_failed_message_is_not_acknowledged_as_success(running):
    module, client, _ = running
    with pytest.raises(LocalServiceError):
        client.send({"reject": True})
    client.send({"accepted": True})
    assert module.messages == [{"accepted": True}]


@pytest.mark.parametrize(
    "message",
    [b"bad\n", b"[]\n", b'{"action":"unknown"}\n', b"x" * 1_048_577],
    ids=["invalid-json", "array", "unknown-action", "oversize"],
)
def test_invalid_requests_are_rejected(running, message):
    _, client, _ = running
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(str(client.socket_path))
        peer.sendall(message)
        assert peer.recv(16) == b"error\n"


def test_worker_failure_stops_service(running):
    module, _, errors = running
    module.fail_tick = True
    assert module.closed.wait(2)


def test_parent_exit_calls_module_then_cleans_socket(socket_path):
    module = RecordingModule()
    path = socket_path
    runtime = ServiceRuntime(
        path, module, is_parent_alive=lambda: False, tick_seconds=0.01
    )
    runtime.run()
    assert module.exited
    assert module.closed.is_set()
    assert not path.exists()


def test_missing_socket_is_explicit(tmp_path):
    with pytest.raises(LocalServiceError):
        SocketClient(tmp_path / "missing").send({})


def test_module_selection_refuses_missing_and_duplicate(monkeypatch):
    from policyengine_local_service import loading

    monkeypatch.setattr(loading, "entry_points", lambda **kwargs: [])
    with pytest.raises(LocalServiceError, match="exactly one"):
        load_module("example", {}, None)
    entry = EntryPoint(
        name="example",
        value="does_not_exist:create",
        group="policyengine.local_service.modules",
    )
    monkeypatch.setattr(loading, "entry_points", lambda **kwargs: [entry, entry])
    with pytest.raises(LocalServiceError, match="exactly one"):
        load_module("example", {}, None)

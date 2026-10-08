"""The service host works without importing any telemetry implementation."""

import socket
import tempfile
import threading
import time
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from microcosm_emitter.host.client import SocketClient
from microcosm_emitter.host.errors import LocalServiceError
from microcosm_emitter.host.loading import load_module
from microcosm_emitter.host.runtime import ServiceRuntime


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
    client = SocketClient(path)
    try:
        deadline = time.monotonic() + 3
        last_error = None
        while time.monotonic() < deadline:
            if errors:
                raise AssertionError(f"Service setup failed: {errors[0]}") from errors[
                    0
                ]
            if not thread.is_alive():
                raise AssertionError("Service exited before answering a readiness ping")
            try:
                client.ping()
            except LocalServiceError as error:
                last_error = error
                time.sleep(0.01)
            else:
                break
        else:
            raise AssertionError(
                "Service did not answer a readiness ping"
            ) from last_error
        yield module, client, errors
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
    from microcosm_emitter.host import loading

    monkeypatch.setattr(loading, "entry_points", lambda **kwargs: [])
    with pytest.raises(LocalServiceError, match="exactly one"):
        load_module("example", {}, None)
    entry = EntryPoint(
        name="example",
        value="does_not_exist:create",
        group="microcosm.emitter.modules",
    )
    monkeypatch.setattr(loading, "entry_points", lambda **kwargs: [entry, entry])
    with pytest.raises(LocalServiceError, match="exactly one"):
        load_module("example", {}, None)


@pytest.fixture
def delayed_listener(monkeypatch):
    """Hold listen until a real readiness probe sees the bound socket refuse it."""
    bound = threading.Event()
    allow_listen = threading.Event()
    probes = []
    original_listen = socket.socket.listen
    original_ping = SocketClient.ping

    def listen(server, backlog):
        bound.set()
        if not allow_listen.wait(3):
            raise TimeoutError("test did not release the listening socket")
        original_listen(server, backlog)

    def ping(client):
        probes.append(client.socket_path)
        if len(probes) == 1:
            assert bound.wait(3), "test service never bound the socket"
            try:
                return original_ping(client)
            finally:
                allow_listen.set()
        return original_ping(client)

    monkeypatch.setattr(socket.socket, "listen", listen)
    monkeypatch.setattr(SocketClient, "ping", ping)
    try:
        yield probes, allow_listen
    finally:
        allow_listen.set()


@pytest.fixture
def delayed_running(delayed_listener, request):
    """Install the delay before starting the ordinary fixture."""
    return request.getfixturevalue("running")


def test_fixture_waits_for_socket_to_accept_requests(delayed_running, delayed_listener):
    probes, allow_listen = delayed_listener
    try:
        assert len(probes) >= 2, "fixture must retry a bound but non-listening socket"
        module, client, errors = delayed_running
        client.send({"ready": True})
        assert module.messages == [{"ready": True}]
        assert not errors
    finally:
        allow_listen.set()


def test_fixture_cleans_up_when_initialization_fails(request, monkeypatch):
    modules = []

    def initialize(module):
        modules.append(module)
        raise RuntimeError("synthetic initialization failure")

    monkeypatch.setattr(RecordingModule, "initialize", initialize)
    with pytest.raises(AssertionError, match="synthetic initialization failure"):
        request.getfixturevalue("running")
    assert len(modules) == 1
    assert modules[0].closed.is_set()

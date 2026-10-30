"""One local host routes named modules and gives each its own worker."""

import threading
import time

from microcosm_emitter.host.client import SocketClient
from microcosm_emitter.host.runtime import ServiceRuntime


class Module:
    def __init__(self):
        self.messages = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def initialize(self):
        pass

    def handle_message(self, message):
        self.messages.append(dict(message))

    def query(self, message):
        return {"result": message["value"]}

    def tick(self, now):
        self.entered.set()
        if self.fail:
            error_message = "synthetic failure"
            raise ValueError(error_message)
        self.release.wait(2)

    def parent_exited(self):
        pass

    def close(self, deadline):
        pass


def test_slow_module_does_not_block_another_worker_or_socket(tmp_path):
    # macOS needs short socket paths; reuse the runtime suite's fixture.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="provider-test-", dir="/tmp") as directory:
        path = Path(directory) / "e.sock"
        slow, other = Module(), Module()
        other.release.set()
        runtime = ServiceRuntime(
            path,
            modules={"publication": slow, "telemetry": other},
            is_parent_alive=lambda: True,
            tick_seconds=0.01,
        )
        thread = threading.Thread(target=runtime.run)
        thread.start()
        try:
            assert slow.entered.wait(2)
            assert other.entered.wait(2)
            client = SocketClient(path)
            client.for_module("telemetry").send({"number": 1})
            assert other.messages == [{"number": 1}]
            assert slow.messages == []
            client.ping()
            assert client.for_module("publication").request({"value": "x" * 5000}) == {
                "result": "x" * 5000
            }
        finally:
            slow.release.set()
            runtime.stop()
            thread.join(3)
        assert not thread.is_alive()


def test_one_worker_failure_does_not_stop_another_module(tmp_path):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="provider-test-", dir="/tmp") as directory:
        path = Path(directory) / "e.sock"
        broken, healthy = Module(), Module()
        broken.fail = True
        healthy.release.set()
        runtime = ServiceRuntime(
            path,
            modules={"broken": broken, "healthy": healthy},
            is_parent_alive=lambda: True,
            tick_seconds=0.01,
        )
        errors = []

        def run():
            try:
                runtime.run()
            # Keeps any exception this thread's call raises, for the test to check.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert broken.entered.wait(2)
            assert healthy.entered.wait(2)
            time.sleep(0.05)
            SocketClient(path).for_module("healthy").send({"accepted": True})
            assert healthy.messages == [{"accepted": True}]
        finally:
            runtime.stop()
            thread.join(3)
        assert not thread.is_alive()
    assert errors


class RequestModule(Module):
    """Track callback ownership independently from the periodic worker."""

    def __init__(self):
        super().__init__()
        self.callback_entered = threading.Event()
        self.callback_release = threading.Event()
        self.closed = threading.Event()
        self.parent_called = threading.Event()
        self.values = []
        self.active = False

    def query(self, message):
        assert not self.active
        self.active = True
        try:
            value = message["value"]
            self.values.append(value)
            if value == "hold":
                self.callback_entered.set()
                assert self.callback_release.wait(5)
            if value == "reject":
                error_message = "synthetic query rejection"
                raise ValueError(error_message)
            return {"result": value}
        finally:
            self.active = False

    def parent_exited(self):
        assert not self.active
        self.parent_called.set()

    def close(self, deadline):
        assert not self.active
        self.closed.set()


def test_bounded_module_queue_preserves_order_and_other_module_control(monkeypatch):
    """A full graph request queue cannot consume telemetry or control capacity."""
    import json
    import socket
    import tempfile
    from pathlib import Path

    import pytest

    from microcosm_emitter.host import runtime as runtime_module
    from microcosm_emitter.host.errors import LocalServiceError

    monkeypatch.setattr(runtime_module, "MAX_PENDING_REQUESTS", 2)
    slow, healthy = RequestModule(), RequestModule()
    healthy.release.set()
    errors, queued = [], []
    with tempfile.TemporaryDirectory(prefix="pe-queue-", dir="/tmp") as raw:
        runtime = ServiceRuntime(
            Path(raw) / "s",
            modules={"slow": slow, "healthy": healthy},
            is_parent_alive=lambda: True,
            tick_seconds=100,
            drain_seconds=1,
        )

        def serve():
            try:
                runtime.run()
            # Keeps any exception this thread's call raises, for the test to check.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        client = SocketClient(runtime.socket_path)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    client.ping()
                    break
                except LocalServiceError:
                    time.sleep(0.01)
            for value in ("hold", "second", "third"):
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.settimeout(3)
                peer.connect(str(runtime.socket_path))
                peer.sendall(
                    json.dumps(
                        {
                            "action": "query",
                            "module": "slow",
                            "message": {"value": value},
                        }
                    ).encode()
                    + b"\n"
                )
                queued.append(peer)
                if value == "hold":
                    assert slow.callback_entered.wait(3)
                else:
                    target_size = len(queued) - 1
                    deadline = time.monotonic() + 3
                    while (
                        runtime._requests._queues["slow"].qsize() < target_size
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    assert runtime._requests._queues["slow"].qsize() == target_size
            with pytest.raises(LocalServiceError):
                client.for_module("slow").request({"value": "overflow"})
            client.for_module("healthy").send({"accepted": True})
            assert healthy.messages == [{"accepted": True}]
            client.ping()
            client.close()
            assert not slow.closed.is_set() and not healthy.closed.is_set()
            slow.callback_release.set()
            for peer, value in zip(queued, ("hold", "second", "third"), strict=True):
                assert json.loads(peer.recv(4096)) == {"result": value}
            thread.join(3)
            assert not thread.is_alive() and not errors
            assert slow.values == ["hold", "second", "third"]
            assert slow.closed.is_set() and healthy.messages == [{"accepted": True}]
        finally:
            slow.callback_release.set()
            runtime.stop()
            for peer in queued:
                peer.close()
            thread.join(3)


def test_parent_exit_waits_for_active_requests_before_lifecycle_callbacks():
    """Parent-exit callbacks and close must follow accepted module work."""
    import json
    import socket
    import tempfile
    from pathlib import Path

    from microcosm_emitter.host.errors import LocalServiceError

    module, alive, errors = RequestModule(), threading.Event(), []
    alive.set()
    with tempfile.TemporaryDirectory(prefix="pe-parent-", dir="/tmp") as raw:
        runtime = ServiceRuntime(
            Path(raw) / "s",
            module,
            is_parent_alive=alive.is_set,
            tick_seconds=100,
            drain_seconds=1,
        )

        def serve():
            try:
                runtime.run()
            # Keeps any exception this thread's call raises, for the test to check.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        client = SocketClient(runtime.socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        client.ping()
                        break
                    except LocalServiceError:
                        time.sleep(0.01)
                peer.settimeout(3)
                peer.connect(str(runtime.socket_path))
                peer.sendall(b'{"action":"query","message":{"value":"hold"}}\n')
                assert module.callback_entered.wait(3)
                alive.clear()
                deadline = time.monotonic() + 3
                while not runtime._parent_exited and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert runtime._parent_exited
                assert not module.parent_called.is_set() and not module.closed.is_set()
                module.callback_release.set()
                assert json.loads(peer.recv(4096)) == {"result": "hold"}
                thread.join(3)
                assert not thread.is_alive() and not errors
                assert module.parent_called.is_set() and module.closed.is_set()
            finally:
                module.callback_release.set()
                runtime.stop()
                thread.join(3)


def test_shutdown_timeout_does_not_close_an_active_callback():
    """Shutdown stays bounded without disposing resources used by a stuck request."""
    import socket
    import tempfile
    from pathlib import Path

    from microcosm_emitter.host.errors import LocalServiceError

    module, errors = RequestModule(), []
    with tempfile.TemporaryDirectory(prefix="pe-drain-", dir="/tmp") as raw:
        runtime = ServiceRuntime(
            Path(raw) / "s",
            module,
            is_parent_alive=lambda: True,
            tick_seconds=100,
            drain_seconds=0,
        )

        def serve():
            try:
                runtime.run()
            # Keeps any exception this thread's call raises, for the test to check.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        client = SocketClient(runtime.socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        client.ping()
                        break
                    except LocalServiceError:
                        time.sleep(0.01)
                peer.connect(str(runtime.socket_path))
                peer.sendall(b'{"action":"query","message":{"value":"hold"}}\n')
                assert module.callback_entered.wait(3)
                client.close()
                thread.join(1)
                assert not thread.is_alive()
                assert len(errors) == 1 and isinstance(errors[0], LocalServiceError)
                assert not module.closed.is_set()
                module.callback_release.set()
                assert module.closed.wait(3)
            finally:
                module.callback_release.set()
                runtime.stop()
                thread.join(3)

"""One local host routes named modules and gives each its own worker."""

import threading
import time

from microcosm_provider_client.client import SocketClient
from microcosm_provider_client.runtime import ServiceRuntime


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

    def tick(self, now):
        self.entered.set()
        if self.fail:
            raise ValueError("synthetic failure")
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
            except Exception as error:
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

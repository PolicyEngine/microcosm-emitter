"""Loopback services shared by the interface tests; nothing leaves this machine."""

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fake_hugging_face import (
    PRIVATE_REPOSITORY,
    STAGING_REPOSITORY,
    FakeHuggingFaceApi,
)


@pytest.fixture
def loopback_collector():
    """Serve a fake collector that records each request's path and bearer token.

    Yields the collector's URL and the list of requests it received.
    """
    received_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received_requests.append(
                (self.path, self.headers["Authorization"], payload)
            )
            if self.path.endswith("/exchange"):
                status_code, response = (
                    200,
                    {"access_token": "collector-session-for-tests", "expires_in": 3600},
                )
            elif self.path == "/v1/runs":
                status_code, response = 201, {"registered": True}
            else:
                status_code, response = (
                    202,
                    {"accepted": len(payload["events"]), "duplicates": 0},
                )
            body = json.dumps(response).encode()
            self.send_response(status_code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received_requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def emitter_command_script(tmp_path):
    """Write an executable stand-in for the installed command."""

    def write(name: str, python_source: str) -> Path:
        path = tmp_path / "commands" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n{python_source}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    return write


@pytest.fixture
def isolated_path(monkeypatch, tmp_path):
    """Set PATH to only one fresh directory, and return that directory."""
    directory = tmp_path / "path-directory"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


@dataclass
class InThreadEmitter:
    """An emitter host running in a thread of the test process."""

    transport: object
    description: dict = field(default_factory=dict)

    def close(self):
        self.transport.close()


@pytest.fixture
def in_thread_emitter():
    return InThreadEmitter


@pytest.fixture
def emitter_with_hub(tmp_path):
    """Yield a client talking to the module over a real socket, and an in-memory Hub."""
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.host.runtime import ServiceRuntime
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule
    from microcosm_emitter_interface.client import EmitterClient
    from microcosm_emitter_interface.transport import SocketTransport

    hub = FakeHuggingFaceApi(whoami={"auth": {"accessToken": {"role": "write"}}})
    hub.create_repository(STAGING_REPOSITORY)
    hub.create_repository(PRIVATE_REPOSITORY)
    runtime_directory = Path(tempfile.mkdtemp(prefix="pe-stage-", dir="/tmp"))
    socket_path = runtime_directory / "service.sock"
    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
    )
    runtime = ServiceRuntime(
        socket_path,
        modules={"huggingface": module},
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    transport = SocketTransport(socket_path)
    deadline = time.monotonic() + 5
    while True:
        try:
            transport.ping()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    client = EmitterClient(
        InThreadEmitter(
            transport, {"modules": ["huggingface"], "protocol_versions": [1]}
        )
    )
    try:
        yield client, hub
    finally:
        client.close()
        thread.join(timeout=20)
        shutil.rmtree(runtime_directory, ignore_errors=True)

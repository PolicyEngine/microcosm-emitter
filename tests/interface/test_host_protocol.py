"""The launcher and the socket transport send exactly the recorded host fixtures.

These fixtures pin what an older interface release sends to whichever emitter
is installed: the command's arguments, the configuration object on standard
input and each socket request's envelope. An argument-parser or envelope change
made on both sides at once would pass every other test while breaking builds
that still run an older interface.
"""

import json
import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess
from microcosm_emitter_interface.orrery import OrrerySettings
from microcosm_emitter_interface.protocol_fixtures import (
    ProtocolFixture,
    materialize,
    protocol_fixtures,
)
from microcosm_emitter_interface.transport import SocketTransport

HELLO_ANSWER = {
    "emitter_version": "0.0.0",
    "protocol_versions": [1],
    "modules": ["orrery", "telemetry"],
    "skipped_modules": [],
}


def fixtures_by_name() -> dict[str, ProtocolFixture]:
    return {fixture.name: fixture for fixture in protocol_fixtures()}


class RecordingSocketServer:
    """A Unix socket that records each request line and answers like a host."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.requests: list[bytes] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(4)
        self._server.settimeout(0.1)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve)
        self._thread.start()

    def _serve(self):
        while not self._stopped.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(2)
                request = bytearray()
                while not request.endswith(b"\n"):
                    part = connection.recv(65_536)
                    if not part:
                        break
                    request.extend(part)
                self.requests.append(bytes(request))
                action = json.loads(request)["action"]
                if action == "hello":
                    answer = json.dumps(HELLO_ANSWER).encode() + b"\n"
                elif action == "query":
                    answer = b'{"receipt": null}\n'
                else:
                    answer = b"ok\n"
                connection.sendall(answer)

    def close(self):
        self._stopped.set()
        self._thread.join(timeout=5)
        self._server.close()


@pytest.fixture
def recording_server():
    # Short paths are necessary for macOS's Unix-socket path limit.
    directory = Path(tempfile.mkdtemp(prefix="pe-fixture-", dir="/tmp"))
    server = RecordingSocketServer(directory / "service.sock")
    try:
        yield server
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)


def client_for(transport: SocketTransport) -> EmitterClient:
    return EmitterClient(
        EmitterProcess(
            process=None,
            transport=transport,
            runtime_directory=Path("/nonexistent"),
            description=dict(HELLO_ANSWER),
        )
    )


ENVELOPE_SENT_BY: dict[str, Callable[[SocketTransport], Any]] = {
    "host-ping": lambda transport: transport.ping(timeout=2),
    "host-hello": lambda transport: transport.hello(timeout=2),
    "host-message-envelope": lambda transport: client_for(
        transport
    ).telemetry.complete(),
    "host-query-envelope": lambda transport: client_for(transport).orrery_receipt(
        "fixture-publication"
    ),
    "host-close": lambda transport: transport.close(),
}


def test_every_host_fixture_has_a_test_that_sends_it():
    host_fixtures = {
        fixture.name for fixture in protocol_fixtures() if fixture.module == "host"
    }
    assert host_fixtures == set(ENVELOPE_SENT_BY) | {"host-launch"}


@pytest.mark.parametrize("name", sorted(ENVELOPE_SENT_BY))
def test_socket_request_has_the_recorded_envelope(recording_server, name):
    fixture = fixtures_by_name()[name]
    transport = SocketTransport(recording_server.socket_path, timeout=2)
    ENVELOPE_SENT_BY[name](transport)
    (request,) = recording_server.requests
    # One JSON object per request, ended by a newline.
    assert request.endswith(b"\n")
    assert request.count(b"\n") == 1
    assert json.loads(request) == fixture.body


def test_launcher_passes_the_recorded_arguments_and_standard_input(
    tmp_path, emitter_command_script
):
    record_path = tmp_path / "launch-record.json"
    recorder = emitter_command_script(
        "recording-emitter",
        "import json, sys\n"
        "standard_input = json.loads(sys.stdin.read())\n"
        f"with open({str(record_path)!r}, 'w') as record:\n"
        "    json.dump({'arguments': sys.argv[1:],"
        " 'standard_input': standard_input}, record)\n"
        "sys.exit(3)\n",
    )
    spool_path = tmp_path / "events.sqlite3"
    client = EmitterClient.start(
        orrery=OrrerySettings(
            development_collector_url="http://127.0.0.1:1",
            publication_origin="http://127.0.0.1:1",
        ),
        spool_path=spool_path,
        command=recorder,
        startup_timeout=10,
    )
    assert client.startup_error is not None
    assert client.startup_error.reason == "child_exit"
    recorded = json.loads(record_path.read_text())
    socket_path = Path(recorded["arguments"][2])
    assert socket_path.name == "service.sock"
    expected = materialize(
        fixtures_by_name()["host-launch"],
        directory=tmp_path,
        spool_path=spool_path,
        socket_path=socket_path,
        parent_pid=os.getpid(),
    )
    assert recorded == expected

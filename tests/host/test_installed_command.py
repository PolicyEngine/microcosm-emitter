"""The installed `microcosm-emitter` command runs the host as its caller's child."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from importlib.metadata import version
from pathlib import Path

import psutil
import pytest

from microcosm_emitter.host.runtime import ServiceRuntime

COMMAND_NAME = "microcosm-emitter"


def supported_protocol_versions() -> list[int]:
    from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS

    return list(SUPPORTED_PROTOCOL_VERSIONS)


def installed_command() -> str:
    command = shutil.which(COMMAND_NAME)
    assert command is not None, f"{COMMAND_NAME} is not on PATH"
    return command


def orrery_configuration(spool_path: Path) -> dict:
    """Return the configuration of a real Orrery module.

    The module's idle worker makes no network requests.
    """
    return {
        "spool_path": str(spool_path),
        "development_collector_url": "http://127.0.0.1:1",
        "publication_origin": "http://127.0.0.1:1",
    }


def socket_request(socket_path: Path, payload: dict, *, timeout: float = 2.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(timeout)
        peer.connect(str(socket_path))
        peer.sendall(json.dumps(payload).encode() + b"\n")
        response = bytearray()
        while not response.endswith(b"\n"):
            part = peer.recv(65_536)
            if not part:
                break
            response.extend(part)
    return bytes(response)


def wait_until_ready(process: subprocess.Popen, socket_path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error_message = f"host exited early with {process.returncode}"
            raise AssertionError(error_message)
        try:
            if socket_request(socket_path, {"action": "ping"}) == b"ok\n":
                return
        except OSError:
            pass
        time.sleep(0.05)
    error_message = "host did not become ready"
    raise AssertionError(error_message)


def start_command(
    configurations: dict,
    runtime_directory: Path,
    *,
    extra_arguments: tuple[str, ...] = (),
    parent_pid: int | None = None,
) -> tuple[subprocess.Popen, Path]:
    socket_path = runtime_directory / "service.sock"
    reader, writer = socket.socketpair()
    with reader, writer:
        process = subprocess.Popen(
            [
                installed_command(),
                "host",
                "--socket",
                str(socket_path),
                "--parent-pid",
                str(os.getpid() if parent_pid is None else parent_pid),
                *extra_arguments,
            ],
            stdin=reader,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        writer.sendall(json.dumps(configurations).encode())
        writer.shutdown(socket.SHUT_WR)
    return process, socket_path


@pytest.fixture
def runtime_directories():
    """Fresh private directories; a host removes its own directory on exit."""
    created = []

    def create() -> Path:
        # Short paths are necessary for macOS's Unix-socket path limit.
        directory = Path(tempfile.mkdtemp(prefix="pe-cmd-", dir="/tmp"))
        created.append(directory)
        return directory

    try:
        yield create
    finally:
        for directory in created:
            shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def runtime_directory(runtime_directories):
    return runtime_directories()


def stop(process: subprocess.Popen, socket_path: Path) -> int:
    with contextlib.suppress(OSError):
        socket_request(socket_path, {"action": "close"})
    try:
        return process.wait(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()


def test_installed_command_runs_the_host_and_reports_its_protocol_versions(
    tmp_path, runtime_directory
):
    process, socket_path = start_command(
        {"orrery": orrery_configuration(tmp_path / "events.sqlite3")},
        runtime_directory,
    )
    try:
        wait_until_ready(process, socket_path)
        description = json.loads(socket_request(socket_path, {"action": "hello"}))
        assert description == {
            "emitter_version": version("microcosm-emitter"),
            "protocol_versions": supported_protocol_versions(),
            "modules": ["orrery"],
            "skipped_modules": [],
        }
    finally:
        assert stop(process, socket_path) == 0


def test_hello_and_version_payloads_have_their_typed_fields():
    from microcosm_emitter.cli import describe
    from microcosm_emitter.compatibility import EmitterDescription, HelloResponse

    assert set(describe()) == EmitterDescription.__required_keys__
    assert HelloResponse.__required_keys__ == {
        "emitter_version",
        "protocol_versions",
        "modules",
        "skipped_modules",
    }


def test_host_without_a_parent_creation_time_reads_it_from_its_own_parent(
    tmp_path, runtime_directory
):
    """A short-lived build process starts the host; the host exits after it."""
    launcher_script = tmp_path / "short_lived_build.py"
    launcher_script.write_text(
        textwrap.dedent(
            """
            import json, os, socket, subprocess, sys, time
            import psutil
            command, socket_path, spool_path = sys.argv[1:4]
            reader, writer = socket.socketpair()
            with reader, writer:
                # The host must not inherit this script's output pipes: the
                # test reads them to the end, which would otherwise wait for
                # the host to exit and hide whether it outlives this script.
                host = subprocess.Popen(
                    [command, "host", "--socket", socket_path,
                     "--parent-pid", str(os.getpid())],
                    stdin=reader, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                writer.sendall(json.dumps({"orrery": {
                    "spool_path": spool_path,
                    "development_collector_url": "http://127.0.0.1:1",
                    "publication_origin": "http://127.0.0.1:1",
                }}).encode())
                writer.shutdown(socket.SHUT_WR)
            ready = False
            deadline = time.monotonic() + 15
            while not ready and time.monotonic() < deadline:
                try:
                    with socket.socket(socket.AF_UNIX) as peer:
                        peer.connect(socket_path)
                        peer.sendall(b'{"action":"ping"}\\n')
                        ready = peer.recv(16) == b"ok\\n"
                except OSError:
                    time.sleep(0.05)
            print(json.dumps({
                "pid": host.pid,
                "created_at": psutil.Process(host.pid).create_time(),
                "ready": ready,
            }), flush=True)
            """
        )
    )
    socket_path = runtime_directory / "service.sock"
    build = subprocess.run(
        [
            sys.executable,
            str(launcher_script),
            installed_command(),
            str(socket_path),
            str(tmp_path / "events.sqlite3"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    started = json.loads(build.stdout)
    assert started["ready"], "the host did not answer while its parent ran"
    try:
        host = psutil.Process(started["pid"])
        is_same_host = host.create_time() == started["created_at"]
    except psutil.NoSuchProcess:
        return  # Already exited after its parent.
    if not is_same_host:
        return  # Exited, and an unrelated process now has its process ID.
    gone, alive = psutil.wait_procs([host], timeout=20)
    for process in alive:
        process.kill()
    assert gone, "the host kept running after its parent exited"


def test_host_refuses_a_parent_process_identifier_that_is_not_its_parent(
    tmp_path, runtime_directory
):
    unrelated_process = 1  # init or launchd, never the test's child's parent
    process, socket_path = start_command(
        {"orrery": orrery_configuration(tmp_path / "events.sqlite3")},
        runtime_directory,
        parent_pid=unrelated_process,
    )
    _, error_output = process.communicate(timeout=20)
    assert process.returncode == 1
    assert b"Service startup failed" in error_output
    assert not socket_path.exists()


def test_unavailable_modules_are_skipped_only_when_requested(
    tmp_path, runtime_directories
):
    configurations = {
        "orrery": orrery_configuration(tmp_path / "events.sqlite3"),
        "not-installed": {},
    }
    refused, _ = start_command(configurations, runtime_directories())
    _, error_output = refused.communicate(timeout=20)
    assert refused.returncode == 1
    assert b"Service startup failed" in error_output

    process, socket_path = start_command(
        configurations,
        runtime_directories(),
        extra_arguments=("--skip-unavailable-modules",),
    )
    try:
        wait_until_ready(process, socket_path)
        description = json.loads(socket_request(socket_path, {"action": "hello"}))
        assert description["modules"] == ["orrery"]
        assert description["skipped_modules"] == ["not-installed"]
        # A request to a skipped module is refused like one to an unknown
        # module, and the host keeps serving the modules it loaded.
        for action in ("message", "query"):
            refusal = socket_request(
                socket_path,
                {"action": action, "module": "not-installed", "message": {}},
            )
            assert refusal == b"error\n"
        assert socket_request(socket_path, {"action": "ping"}) == b"ok\n"
    finally:
        assert stop(process, socket_path) == 0


def test_skipping_unavailable_modules_refuses_to_start_without_any_module(
    runtime_directory,
):
    process, socket_path = start_command(
        {"not-installed": {}, "also-not-installed": {}},
        runtime_directory,
        extra_arguments=("--skip-unavailable-modules",),
    )
    _, error_output = process.communicate(timeout=20)
    assert process.returncode == 1
    assert b"Service startup failed" in error_output
    assert not socket_path.exists()


class RecordedParent:
    """Stand-ins for ``os.getppid`` and ``psutil.Process`` that log each call."""

    def __init__(self, parent_ids: list[int], created_at: float):
        self.parent_ids = list(parent_ids)
        self.created_at = created_at
        self.calls: list[str] = []

    def getppid(self) -> int:
        self.calls.append("getppid")
        return (
            self.parent_ids.pop(0) if len(self.parent_ids) > 1 else self.parent_ids[0]
        )

    def process(self, pid: int):
        recorder = self

        class Process:
            def create_time(self) -> float:
                recorder.calls.append("create_time")
                return recorder.created_at

        return Process()


def test_parent_creation_time_is_read_before_the_final_parent_check(monkeypatch):
    from microcosm_emitter.host import process as process_module

    recorded = RecordedParent([4321], created_at=1234.5)
    monkeypatch.setattr(process_module.os, "getppid", recorded.getppid)
    monkeypatch.setattr(process_module.psutil, "Process", recorded.process)

    parent = process_module.parent_process(4321, None)

    assert (parent.pid, parent.created_at) == (4321, 1234.5)
    assert "create_time" in recorded.calls
    assert recorded.calls[-1] == "getppid"
    assert recorded.calls.index("create_time") < len(recorded.calls) - 1


def test_parent_that_exits_while_its_creation_time_is_read_is_refused(monkeypatch):
    """The process ID may already name an unrelated process when it is read."""
    from microcosm_emitter.host import process as process_module

    # The parent is this host's parent at first, then exits; the host is
    # adopted by process 1 while the creation time is being read.
    recorded = RecordedParent([4321, 1], created_at=1234.5)
    monkeypatch.setattr(process_module.os, "getppid", recorded.getppid)
    monkeypatch.setattr(process_module.psutil, "Process", recorded.process)

    with pytest.raises(ValueError, match="not this host's parent"):
        process_module.parent_process(4321, None)


def test_version_command_prints_the_versions_and_the_collector_origin():
    from microcosm_emitter.core.constants import PRODUCTION_COLLECTOR_URL

    result = subprocess.run(
        [installed_command(), "version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # The Modal runner compares the collector origin with the origin a
    # collector token was obtained for, before it stages any input.
    assert json.loads(result.stdout) == {
        "emitter_version": version("microcosm-emitter"),
        "protocol_versions": supported_protocol_versions(),
        "collector_origin": PRODUCTION_COLLECTOR_URL,
    }


def test_module_entry_keeps_working_for_existing_launchers(tmp_path, runtime_directory):
    """Builds pinned to an earlier emitter run `python -m microcosm_emitter.host`."""
    parent = psutil.Process(os.getpid())
    socket_path = runtime_directory / "service.sock"
    reader, writer = socket.socketpair()
    with reader, writer:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "microcosm_emitter.host",
                "--socket",
                str(socket_path),
                "--parent-pid",
                str(parent.pid),
                "--parent-created-at",
                str(parent.create_time()),
            ],
            stdin=reader,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        )
        writer.sendall(
            json.dumps(
                {"orrery": orrery_configuration(tmp_path / "events.sqlite3")}
            ).encode()
        )
        writer.shutdown(socket.SHUT_WR)
    try:
        wait_until_ready(process, socket_path)
    finally:
        assert stop(process, socket_path) == 0


def wait_for_runtime(socket_path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if socket_request(socket_path, {"action": "ping"}) == b"ok\n":
                return
        except OSError:
            time.sleep(0.01)
    error_message = "runtime did not become ready"
    raise AssertionError(error_message)


class IdleModule:
    def initialize(self):
        pass

    def handle_message(self, message):
        pass

    def tick(self, now):
        pass

    def parent_exited(self):
        pass

    def close(self, deadline):
        pass


def test_hello_returns_the_description_given_to_the_runtime(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="pe-hello-", dir="/tmp"))
    socket_path = directory / "service.sock"
    runtime = ServiceRuntime(
        socket_path,
        modules={"idle": IdleModule()},
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
        description={"modules": ["idle"], "protocol_versions": [1]},
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        wait_for_runtime(socket_path)
        assert json.loads(socket_request(socket_path, {"action": "hello"})) == {
            "modules": ["idle"],
            "protocol_versions": [1],
        }
        assert socket_request(socket_path, {"action": "ping"}) == b"ok\n"
    finally:
        runtime.stop()
        thread.join(timeout=10)
        shutil.rmtree(directory, ignore_errors=True)


def test_hello_without_a_description_is_refused_like_an_unknown_action(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="pe-hello-", dir="/tmp"))
    socket_path = directory / "service.sock"

    runtime = ServiceRuntime(
        socket_path, IdleModule(), is_parent_alive=lambda: True, tick_seconds=0.01
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        wait_for_runtime(socket_path)
        assert socket_request(socket_path, {"action": "hello"}) == b"error\n"
    finally:
        runtime.stop()
        thread.join(timeout=10)
        shutil.rmtree(directory, ignore_errors=True)

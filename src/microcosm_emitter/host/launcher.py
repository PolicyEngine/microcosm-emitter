"""Start a module in the current interpreter, without installing anything."""

import contextlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from microcosm_emitter.host.client import SocketClient
from microcosm_emitter.host.constants import (
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    MODULE_HOST,
    PROCESS_EXIT_TIMEOUT,
    RUNTIME_PREFIX,
    SOCKET_FILENAME,
    STARTUP_POLL,
    STARTUP_TIMEOUT,
)
from microcosm_emitter.host.contracts import JsonObject
from microcosm_emitter.host.errors import (
    LocalServiceError,
    ServiceStartupError,
    StartupFailureReason,
    safe_startup_error,
)


@dataclass
class ServiceHandle:
    """A started host: its child process, socket client and private directory."""

    process: subprocess.Popen
    client: SocketClient
    runtime_dir: Path

    def close(self) -> None:
        """Request shutdown and reap the child without delaying the caller."""
        try:
            self.client.close()
        finally:
            threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        try:
            self.process.wait(timeout=DRAIN_SECONDS + PROCESS_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            terminate_process(self.process)
        finally:
            _remove_runtime_directory(self.runtime_dir)


def start_service(
    module: str, configuration: JsonObject, *, startup_timeout: float = STARTUP_TIMEOUT
) -> ServiceHandle:
    """Start one explicitly selected module."""
    return start_services({module: configuration}, startup_timeout=startup_timeout)


def _configuration_payload(modules: JsonObject, startup_timeout: float) -> bytes:
    """Return the configuration to send to the host, or refuse an invalid one."""
    if (
        not isinstance(startup_timeout, (int, float))
        or not math.isfinite(startup_timeout)
        or startup_timeout < 0
    ):
        startup_reason = "invalid_configuration"
        raise ServiceStartupError(startup_reason)
    if (
        not isinstance(modules, dict)
        or not modules
        or any(
            not isinstance(name, str) or not isinstance(settings, dict)
            for name, settings in modules.items()
        )
    ):
        startup_reason = "invalid_configuration"
        raise ServiceStartupError(startup_reason)
    payload = json.dumps(modules, allow_nan=False).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        startup_reason = "invalid_configuration"
        raise ServiceStartupError(startup_reason)
    return payload


def _send_configuration(writer: socket.socket, payload: bytes, deadline: float) -> None:
    """Write the configuration to the host's standard input before the deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        startup_reason = "configuration_timeout"
        raise ServiceStartupError(startup_reason)
    writer.settimeout(remaining)
    writer.sendall(payload)
    writer.shutdown(socket.SHUT_WR)
    if time.monotonic() >= deadline:
        startup_reason = "configuration_timeout"
        raise ServiceStartupError(startup_reason)


def _wait_for_readiness(
    process: subprocess.Popen, client: SocketClient, deadline: float
) -> None:
    """Return once the host answers a ping before the deadline."""
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            startup_reason = "child_exit"
            raise ServiceStartupError(startup_reason, exit_code=exit_code)
        try:
            client.ping(timeout=min(client.timeout, deadline - time.monotonic()))
        except LocalServiceError:
            pass
        else:
            if time.monotonic() < deadline:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(STARTUP_POLL, remaining))
    startup_reason = "readiness_timeout"
    raise ServiceStartupError(startup_reason)


def start_services(
    modules: JsonObject, *, startup_timeout: float = STARTUP_TIMEOUT
) -> ServiceHandle:
    """Return after readiness, or reap the child and remove temporary files."""
    directory = None
    process = None
    phase: StartupFailureReason = "invalid_configuration"
    try:
        payload = _configuration_payload(modules, startup_timeout)
        deadline = time.monotonic() + startup_timeout
        phase = "startup_failure"
        # Short paths are necessary for macOS's Unix-socket path limit.
        directory = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX, dir="/tmp"))
        directory.chmod(0o700)
        path = directory / SOCKET_FILENAME
        parent = psutil.Process(os.getpid())
        reader, writer = socket.socketpair()
        with reader, writer:
            # No shell: the program is this interpreter running the emitter's own
            # host module, and the other arguments are fixed apart from this
            # run's socket path and this process's ID and start time.
            process = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    MODULE_HOST,
                    "--socket",
                    str(path),
                    "--parent-pid",
                    str(parent.pid),
                    "--parent-created-at",
                    str(parent.create_time()),
                ],
                stdin=reader,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Startup metadata must not appear in process-list arguments.
            phase = "configuration_timeout"
            _send_configuration(writer, payload, deadline)
        phase = "startup_failure"
        client = SocketClient(path)
        _wait_for_readiness(process, client, deadline)
        return ServiceHandle(process, client, directory)
    # Any failure stops the child, removes its directory, and is raised as a
    # startup error with a fixed reason, never as the original exception.
    except Exception as error:  # noqa: BLE001
        # Only transfer timeouts are classified as such; an unrelated writer
        # failure is a startup failure rather than evidence of deadline expiry.
        reason = (
            phase
            if phase != "configuration_timeout" or isinstance(error, TimeoutError)
            else "startup_failure"
        )
        failure = safe_startup_error(error, reason=reason)
        try:
            try:
                if process is not None:
                    terminate_process(process)
            finally:
                if directory is not None:
                    _remove_runtime_directory(directory)
        # A failure while cleaning up is raised as a startup error with a fixed
        # reason, never as the original exception.
        except Exception as cleanup_error:  # noqa: BLE001
            failure = safe_startup_error(cleanup_error)
    # Raise outside the handler so the discarded exception is not retained as
    # either a cause or an implicit context containing private configuration.
    raise failure from None


def terminate_process(process: subprocess.Popen) -> None:
    """Stop ``process`` if it still runs, and wait for it to exit.

    It is terminated first, and killed when it has not exited after
    ``PROCESS_EXIT_TIMEOUT`` seconds.
    """
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT)


def _remove_runtime_directory(directory: Path) -> None:
    (directory / SOCKET_FILENAME).unlink(missing_ok=True)
    # The child removes its own directory on a normal or failed exit.
    with contextlib.suppress(FileNotFoundError):
        directory.rmdir()

"""Start a module in the current interpreter, without installing anything."""

import json
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

from microcosm_provider_client.client import SocketClient
from microcosm_provider_client.constants import (
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    MODULE_HOST,
    PROCESS_EXIT_TIMEOUT,
    RUNTIME_PREFIX,
    SOCKET_FILENAME,
    STARTUP_POLL,
    STARTUP_TIMEOUT,
)
from microcosm_provider_client.contracts import JsonObject
from microcosm_provider_client.errors import LocalServiceError


@dataclass
class ServiceHandle:
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
    """Return after readiness, or reap the child and remove temporary files."""
    directory = None
    process = None
    try:
        payload = json.dumps(configuration, allow_nan=False).encode()
        if len(payload) > MAX_MESSAGE_BYTES:
            raise LocalServiceError("Service configuration is too large.")
        deadline = time.monotonic() + startup_timeout
        # Short paths are necessary for macOS's Unix-socket path limit.
        directory = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX, dir="/tmp"))
        directory.chmod(0o700)
        path = directory / SOCKET_FILENAME
        parent = psutil.Process(os.getpid())
        reader, writer = socket.socketpair()
        with reader, writer:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    MODULE_HOST,
                    "--module",
                    module,
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
            writer.settimeout(max(0.001, deadline - time.monotonic()))
            writer.sendall(payload)
            writer.shutdown(socket.SHUT_WR)
        client = SocketClient(path)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LocalServiceError("Service exited before readiness.")
            try:
                client.ping()
                return ServiceHandle(process, client, directory)
            except LocalServiceError:
                time.sleep(STARTUP_POLL)
        raise LocalServiceError("Service readiness timed out.")
    except Exception as error:
        try:
            if process is not None:
                terminate_process(process)
        finally:
            if directory is not None:
                _remove_runtime_directory(directory)
        if isinstance(error, LocalServiceError):
            raise
        raise LocalServiceError(
            f"Service startup failed ({type(error).__name__})."
        ) from error


def terminate_process(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT)


def _remove_runtime_directory(directory: Path) -> None:
    (directory / SOCKET_FILENAME).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except FileNotFoundError:
        # The child removes its own directory on a normal or failed exit.
        pass

"""Host one explicitly selected service module for one parent process."""

import json
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from policyengine_local_service.constants import (
    ACK_ERROR,
    ACK_OK,
    DELIMITER,
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    READ_BYTES,
    SOCKET_TIMEOUT,
    TICK_SECONDS,
)
from policyengine_local_service.contracts import ServiceModule
from policyengine_local_service.errors import LocalServiceError


class ServiceRuntime:
    def __init__(
        self,
        socket_path: Path,
        module: ServiceModule,
        *,
        is_parent_alive: Callable[[], bool],
        tick_seconds: float = TICK_SECONDS,
        drain_seconds: float = DRAIN_SECONDS,
    ):
        self.socket_path = socket_path
        self.module = module
        self.is_parent_alive = is_parent_alive
        self.tick_seconds = tick_seconds
        self.drain_seconds = drain_seconds
        self._stop = threading.Event()
        self._worker_error: Exception | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        worker = None
        bound = False
        try:
            self.module.initialize()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                # The launcher owns a fresh private directory. Never unlink an
                # existing socket which might belong to another running host.
                server.bind(str(self.socket_path))
                bound = True
                os.chmod(self.socket_path, 0o600)
                server.listen(16)
                server.settimeout(SOCKET_TIMEOUT)
                worker = threading.Thread(target=self._work, daemon=True)
                worker.start()
                while not self._stop.is_set():
                    if not self.is_parent_alive():
                        self.module.parent_exited()
                        break
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        connection.settimeout(SOCKET_TIMEOUT)
                        response = self._respond(connection)
                        try:
                            connection.sendall(response)
                        except OSError:
                            pass
        finally:
            self.stop()
            if worker is not None:
                worker.join(timeout=self.drain_seconds + SOCKET_TIMEOUT)
                if worker.is_alive():
                    self._worker_error = TimeoutError("Service shutdown timed out.")
            else:
                self.module.close(time.monotonic() + self.drain_seconds)
            if bound:
                self.socket_path.unlink(missing_ok=True)
        if self._worker_error is not None:
            raise LocalServiceError(
                f"Service worker failed ({type(self._worker_error).__name__})."
            ) from self._worker_error

    def _respond(self, connection: socket.socket) -> bytes:
        try:
            data = bytearray()
            while DELIMITER not in data:
                chunk = connection.recv(
                    min(READ_BYTES, MAX_MESSAGE_BYTES + 1 - len(data))
                )
                if not chunk:
                    raise ValueError("incomplete request")
                data.extend(chunk)
                if len(data) > MAX_MESSAGE_BYTES:
                    raise ValueError("request too large")
            request = json.loads(bytes(data).split(DELIMITER, 1)[0])
            if not isinstance(request, Mapping):
                raise ValueError("request must be an object")
            action = request.get("action")
            if action == "message":
                message = request.get("message")
                if not isinstance(message, Mapping):
                    raise ValueError("message must be an object")
                self.module.handle_message(message)
            elif action == "close":
                self.stop()
            elif action != "ping":
                raise ValueError("unsupported action")
        except Exception:
            # Module errors reject this request, never acknowledge lost data.
            return ACK_ERROR
        return ACK_OK

    def _work(self) -> None:
        try:
            while not self._stop.wait(self.tick_seconds):
                self.module.tick(time.monotonic())
        except Exception as error:
            self._worker_error = error
            self.stop()
        finally:
            try:
                self.module.close(time.monotonic() + self.drain_seconds)
            except Exception as error:
                self._worker_error = error
                self.stop()

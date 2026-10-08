"""Host explicitly selected modules with independent periodic workers."""

import json
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from microcosm_provider_client.constants import (
    ACK_ERROR,
    ACK_OK,
    DELIMITER,
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    READ_BYTES,
    SOCKET_TIMEOUT,
    TICK_SECONDS,
)
from microcosm_provider_client.contracts import ServiceModule
from microcosm_provider_client.errors import LocalServiceError


class ServiceRuntime:
    def __init__(
        self,
        socket_path: Path,
        module: ServiceModule | None = None,
        *,
        modules: Mapping[str, ServiceModule] | None = None,
        is_parent_alive: Callable[[], bool],
        tick_seconds: float = TICK_SECONDS,
        drain_seconds: float = DRAIN_SECONDS,
    ):
        self.socket_path = socket_path
        if (module is None) == (modules is None):
            raise ValueError("Select one module or an explicit module mapping.")
        self.modules = dict(modules) if modules is not None else {"default": module}
        if not self.modules:
            raise ValueError("At least one module is required.")
        self.module = module
        self.is_parent_alive = is_parent_alive
        self.tick_seconds = tick_seconds
        self.drain_seconds = drain_seconds
        self._stop = threading.Event()
        self._worker_error: Exception | None = None
        self._failed: set[str] = set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        workers = {}
        initialized = []
        bound = False
        try:
            for module in self.modules.values():
                initialized.append(module)
                module.initialize()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                # The launcher owns a fresh private directory. Never unlink an
                # existing socket which might belong to another running host.
                server.bind(str(self.socket_path))
                bound = True
                os.chmod(self.socket_path, 0o600)
                server.listen(16)
                server.settimeout(SOCKET_TIMEOUT)
                for name, module in self.modules.items():
                    worker = threading.Thread(
                        target=self._work, args=(name, module), daemon=True
                    )
                    workers[name] = worker
                    worker.start()
                while not self._stop.is_set():
                    if not self.is_parent_alive():
                        for name, module in self.modules.items():
                            if name not in self._failed:
                                module.parent_exited()
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
            deadline = time.monotonic() + self.drain_seconds + SOCKET_TIMEOUT
            for worker in workers.values():
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    self._worker_error = TimeoutError("Service shutdown timed out.")
            if not workers:
                for module in initialized:
                    module.close(deadline)
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
                name = request.get("module")
                if name is None and len(self.modules) == 1:
                    name = next(iter(self.modules))
                if name not in self.modules or name in self._failed:
                    raise ValueError("module is unavailable")
                self.modules[name].handle_message(message)
            elif action == "close":
                self.stop()
            elif action != "ping":
                raise ValueError("unsupported action")
        except Exception:
            # Module errors reject this request, never acknowledge lost data.
            return ACK_ERROR
        return ACK_OK

    def _work(self, name: str, module: ServiceModule) -> None:
        try:
            while not self._stop.wait(self.tick_seconds):
                module.tick(time.monotonic())
        except Exception as error:
            self._worker_error = error
            self._failed.add(name)
            if len(self._failed) == len(self.modules):
                self.stop()
        finally:
            try:
                module.close(time.monotonic() + self.drain_seconds)
            except Exception as error:
                self._worker_error = error
                self._failed.add(name)
                if len(self._failed) == len(self.modules):
                    self.stop()

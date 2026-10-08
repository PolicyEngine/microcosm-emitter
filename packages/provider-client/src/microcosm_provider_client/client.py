"""Bounded, private Unix-socket transport without domain-specific behavior."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from microcosm_provider_client.constants import (
    ACK_OK,
    DELIMITER,
    MAX_MESSAGE_BYTES,
    SEND_TIMEOUT,
)
from microcosm_provider_client.contracts import JsonObject
from microcosm_provider_client.errors import LocalServiceError


class SocketClient:
    def __init__(self, socket_path: Path, *, timeout: float = SEND_TIMEOUT):
        self.socket_path = socket_path
        self.timeout = timeout
        self.closed = False

    def _request(self, payload: JsonObject) -> None:
        try:
            data = (
                json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                + DELIMITER
            )
            if len(data) > MAX_MESSAGE_BYTES:
                raise ValueError("local message exceeds size limit")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(self.timeout)
                peer.connect(str(self.socket_path))
                peer.sendall(data)
                response = bytearray()
                while DELIMITER not in response and len(response) < 16:
                    part = peer.recv(16 - len(response))
                    if not part:
                        break
                    response.extend(part)
                if bytes(response) != ACK_OK:
                    raise ValueError("service rejected the request")
        except (OSError, ValueError, TypeError) as error:
            raise LocalServiceError(type(error).__name__) from error

    def ping(self) -> None:
        self._request({"action": "ping"})

    def for_module(self, name: str) -> ModuleClient:
        """Route messages without giving a component ownership of host shutdown."""
        return ModuleClient(self, name)

    def send(self, message: JsonObject) -> None:
        if self.closed:
            raise LocalServiceError("client is closed")
        self._request({"action": "message", "message": message})

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._request({"action": "close"})
        finally:
            self.closed = True


class ModuleClient:
    """One selected module's transport; the enclosing ServiceHandle owns shutdown."""

    def __init__(self, client: SocketClient, name: str):
        self.client, self.name = client, name

    def send(self, message: JsonObject) -> None:
        if self.client.closed:
            raise LocalServiceError("client is closed")
        self.client._request(
            {"action": "message", "module": self.name, "message": message}
        )

    def close(self) -> None:
        """A domain component cannot close the shared host."""

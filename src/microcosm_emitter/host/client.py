"""Bounded, private Unix-socket transport without domain-specific behavior."""

from __future__ import annotations

import json
import socket
import time
from typing import TYPE_CHECKING

from microcosm_emitter.host.constants import (
    ACK_OK,
    DELIMITER,
    MAX_MESSAGE_BYTES,
    SEND_TIMEOUT,
)
from microcosm_emitter.host.errors import LocalServiceError

if TYPE_CHECKING:
    from pathlib import Path

    from microcosm_emitter.host.contracts import JsonObject


def _encode_request(payload: JsonObject) -> bytes:
    data = (
        json.dumps(payload, separators=(",", ":"), allow_nan=False).encode() + DELIMITER
    )
    if len(data) > MAX_MESSAGE_BYTES:
        error_message = "local message exceeds size limit"
        raise ValueError(error_message)
    return data


def _check_response_size(response: bytearray) -> None:
    if len(response) > MAX_MESSAGE_BYTES:
        error_message = "local response exceeds size limit"
        raise ValueError(error_message)


def _decode_response(response: bytearray) -> JsonObject | None:
    if bytes(response) == ACK_OK:
        return None
    if not response.endswith(DELIMITER):
        error_message = "incomplete response"
        raise ValueError(error_message)
    result = json.loads(bytes(response).removesuffix(DELIMITER))
    if not isinstance(result, dict):
        error_message = "service rejected the request"
        # The caller reports the exception's type name, so it stays ValueError.
        raise ValueError(error_message)  # noqa: TRY004
    return result


class SocketClient:
    """Client of the local service host's Unix socket, with one connection per request.

    Socket, encoding and decoding errors are raised as ``LocalServiceError``.
    """

    def __init__(self, socket_path: Path, *, timeout: float = SEND_TIMEOUT):
        """Hold the socket path and the default timeout of each socket operation."""
        self.socket_path = socket_path
        self.timeout = timeout
        self.closed = False

    def _request(
        self,
        payload: JsonObject,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> JsonObject | None:
        try:
            data = _encode_request(payload)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:

                def bound_operation():
                    remaining = self.timeout if timeout is None else timeout
                    if deadline is not None:
                        remaining = min(remaining, deadline - time.monotonic())
                        if remaining <= 0:
                            raise TimeoutError
                    peer.settimeout(remaining)

                bound_operation()
                peer.connect(str(self.socket_path))
                bound_operation()
                peer.sendall(data)
                response = bytearray()
                while DELIMITER not in response:
                    bound_operation()
                    part = peer.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(response)))
                    if not part:
                        break
                    response.extend(part)
                    _check_response_size(response)
                return _decode_response(response)
        except (OSError, ValueError, TypeError) as error:
            raise LocalServiceError(type(error).__name__) from error

    def ping(self, *, timeout: float | None = None) -> None:
        """Bound a readiness probe across connection, send and all reads."""
        self._request(
            {"action": "ping"},
            timeout=timeout,
            deadline=None if timeout is None else time.monotonic() + timeout,
        )

    def for_module(self, name: str) -> ModuleClient:
        """Route messages without giving a component ownership of host shutdown."""
        return ModuleClient(self, name)

    def request(
        self, message: JsonObject, *, module: str, timeout: float | None = None
    ) -> JsonObject:
        """Return one bounded service result after its request has completed."""
        if self.closed:
            error_message = "client is closed"
            raise LocalServiceError(error_message)
        response = self._request(
            {"action": "query", "module": module, "message": message}, timeout=timeout
        )
        if response is None:
            error_message = "service did not return a result"
            raise LocalServiceError(error_message)
        return response

    def send(self, message: JsonObject) -> None:
        """Send ``message`` to the host's only module and wait for its acknowledgement.

        Raises ``LocalServiceError`` when the client is closed or the host refuses
        the message.
        """
        if self.closed:
            error_message = "client is closed"
            raise LocalServiceError(error_message)
        self._request({"action": "message", "message": message})

    def close(self) -> None:
        """Ask the host to stop; the client counts as closed even if that fails."""
        if self.closed:
            return
        try:
            self._request({"action": "close"})
        finally:
            self.closed = True


class ModuleClient:
    """One selected module's transport; the enclosing ServiceHandle owns shutdown."""

    def __init__(self, client: SocketClient, name: str):
        """Hold the shared client and the name of the module that messages go to."""
        self.client, self.name = client, name

    def send(self, message: JsonObject) -> None:
        """Send ``message`` to this module and wait for its acknowledgement.

        Raises ``LocalServiceError`` when the shared client is closed or the host
        refuses the message.
        """
        if self.client.closed:
            error_message = "client is closed"
            raise LocalServiceError(error_message)
        self.client._request(
            {"action": "message", "module": self.name, "message": message}
        )

    def request(
        self, message: JsonObject, *, timeout: float | None = None
    ) -> JsonObject:
        """Return this module's result for ``message``."""
        return self.client.request(message, module=self.name, timeout=timeout)

    def close(self) -> None:
        """Do nothing: a domain component cannot close the shared host."""

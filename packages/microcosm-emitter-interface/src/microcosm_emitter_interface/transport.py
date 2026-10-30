"""Bounded requests over the emitter's private Unix socket."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from microcosm_emitter_interface.protocol import (
    ACK_OK,
    ACKNOWLEDGEMENT_TIMEOUT_SECONDS,
    DELIMITER,
    MAX_MESSAGE_BYTES,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class LocalEmitterError(RuntimeError):
    """The local emitter could not start, accept a request, or answer it."""


def _encode_request(payload: Mapping[str, Any]) -> bytes:
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


def _complete_response(response: bytearray) -> bytes:
    if not response.endswith(DELIMITER):
        error_message = "incomplete response"
        raise ValueError(error_message)
    return bytes(response)


class SocketTransport:
    """One connection per request; each step shares the request's time limit."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout: float = ACKNOWLEDGEMENT_TIMEOUT_SECONDS,
    ) -> None:
        """Hold the socket's path and the time limit used when a request gives none."""
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.closed = False

    def _exchange(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> bytes:
        try:
            data = _encode_request(payload)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:

                def bound_next_step() -> None:
                    remaining = self.timeout if timeout is None else timeout
                    if deadline is not None:
                        remaining = min(remaining, deadline - time.monotonic())
                        if remaining <= 0:
                            raise TimeoutError
                    peer.settimeout(remaining)

                bound_next_step()
                peer.connect(str(self.socket_path))
                bound_next_step()
                peer.sendall(data)
                response = bytearray()
                while DELIMITER not in response:
                    bound_next_step()
                    part = peer.recv(min(65_536, MAX_MESSAGE_BYTES + 1 - len(response)))
                    if not part:
                        break
                    response.extend(part)
                    _check_response_size(response)
                return _complete_response(response)
        except (OSError, ValueError, TypeError) as error:
            raise LocalEmitterError(type(error).__name__) from error

    def _acknowledged(self, payload: Mapping[str, Any], **limits: float | None) -> None:
        if self._exchange(payload, **limits) != ACK_OK:
            error_message = "emitter refused the request"
            raise LocalEmitterError(error_message)

    def _answered(
        self, payload: Mapping[str, Any], **limits: float | None
    ) -> dict[str, Any]:
        response = self._exchange(payload, **limits)
        try:
            result = json.loads(response.removesuffix(DELIMITER))
        except ValueError as error:
            error_message = "emitter refused the request"
            raise LocalEmitterError(error_message) from error
        if not isinstance(result, dict):
            error_message = "emitter refused the request"
            raise LocalEmitterError(error_message)
        return result

    def ping(self, *, timeout: float | None = None) -> None:
        """Bound a readiness probe across connection, send and all reads."""
        self._acknowledged(
            {"action": "ping"},
            timeout=timeout,
            deadline=None if timeout is None else time.monotonic() + timeout,
        )

    def hello(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Return the emitter's ``hello`` answer: its version, protocols and modules.

        The protocol requires the keys ``emitter_version``,
        ``protocol_versions`` (those accepted), ``modules`` (those loaded) and
        ``skipped_modules``; an answer may hold more. A given ``timeout``
        bounds the connection, the send and all reads together.
        """
        return self._answered(
            {"action": "hello"},
            timeout=timeout,
            deadline=None if timeout is None else time.monotonic() + timeout,
        )

    def send(
        self, module: str, message: Mapping[str, Any], *, timeout: float | None = None
    ) -> None:
        """Return only after the module has handled and committed the message."""
        if self.closed:
            error_message = "transport is closed"
            raise LocalEmitterError(error_message)
        self._acknowledged(
            {"action": "message", "module": module, "message": dict(message)},
            timeout=timeout,
        )

    def query(
        self, module: str, message: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Return the module's answer after its request has completed."""
        if self.closed:
            error_message = "transport is closed"
            raise LocalEmitterError(error_message)
        return self._answered(
            {"action": "query", "module": module, "message": dict(message)},
            timeout=timeout,
        )

    def close(self) -> None:
        """Ask the host to drain and stop; repeated calls do nothing."""
        if self.closed:
            return
        try:
            self._acknowledged({"action": "close"})
        finally:
            self.closed = True

"""Start the installed ``microcosm-emitter`` command as the build's child process.

The child is started exactly as the emitter's in-process launcher starts
``python -m microcosm_emitter.host``: no ``env`` argument, so it inherits every
environment variable of the build (including ``HF_TOKEN``, ``HUGGINGFACE_TOKEN``
and ``HF_TOKEN_PATH``) and runs as the same user. Its module configuration
travels on standard input, never in process-list arguments. It is never a
separately started system service, which would not see the build's environment.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from microcosm_emitter_interface.protocol import (
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    PROCESS_EXIT_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    RUNTIME_PREFIX,
    SOCKET_FILENAME,
    STARTUP_POLL_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
)
from microcosm_emitter_interface.transport import LocalEmitterError, SocketTransport

#: Names the emitter command when it is not on ``PATH`` (for example a
#: ``uv tool`` directory outside the build's ``PATH``).
COMMAND_ENVIRONMENT_VARIABLE: Final = "MICROCOSM_EMITTER_COMMAND"
DEFAULT_COMMAND: Final = "microcosm-emitter"

type StartupFailureReason = Literal[
    "command_not_found",
    "invalid_configuration",
    "configuration_timeout",
    "child_exit",
    "readiness_timeout",
    "unsupported_protocol_version",
    "startup_failure",
]

_STARTUP_MESSAGES: Final = {
    "command_not_found": "The microcosm-emitter command was not found",
    "invalid_configuration": "Emitter configuration is invalid or too large",
    "configuration_timeout": "Emitter configuration transfer timed out",
    "child_exit": "The emitter exited before readiness",
    "readiness_timeout": "Emitter readiness timed out",
    "unsupported_protocol_version": (
        f"The installed emitter does not accept protocol version {PROTOCOL_VERSION}"
    ),
    "startup_failure": "Emitter startup failed",
}


class EmitterStartupError(LocalEmitterError):
    """A fixed startup reason and optional exit code, never exception details."""

    def __init__(
        self, reason: StartupFailureReason, *, exit_code: int | None = None
    ) -> None:
        """Hold the reason and an integer exit code; build the fixed message.

        An ``exit_code`` whose type is not exactly ``int`` is stored as None.
        An unknown ``reason`` raises ``ValueError``.
        """
        if reason not in _STARTUP_MESSAGES:
            error_message = "Unknown startup failure reason."
            raise ValueError(error_message)
        self.reason: StartupFailureReason = reason
        self.exit_code = exit_code if type(exit_code) is int else None
        details = [reason]
        if self.exit_code is not None:
            details.append(f"exit_code={self.exit_code}")
        super().__init__(f"{_STARTUP_MESSAGES[reason]} ({', '.join(details)}).")


def resolve_command(command: str | os.PathLike[str] | None = None) -> str:
    """Return the emitter command: the argument, then the environment, then ``PATH``.

    The name chosen from ``command``, ``MICROCOSM_EMITTER_COMMAND`` or the
    default ``microcosm-emitter`` is resolved with ``shutil.which``. Raise
    ``EmitterStartupError`` (``command_not_found``) when it does not resolve.
    """
    candidate = (
        str(command)
        if command is not None
        else os.environ.get(COMMAND_ENVIRONMENT_VARIABLE, "").strip() or DEFAULT_COMMAND
    )
    resolved = shutil.which(candidate)
    if resolved is None:
        startup_reason = "command_not_found"
        raise EmitterStartupError(startup_reason)
    return resolved


@dataclass
class EmitterProcess:
    """A running emitter host owned by this build process."""

    process: subprocess.Popen
    transport: SocketTransport
    runtime_directory: Path
    description: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        """Ask the host to drain and stop, then reap it without blocking."""
        try:
            self.transport.close()
        except LocalEmitterError:
            pass
        finally:
            threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        try:
            self.process.wait(timeout=DRAIN_SECONDS + PROCESS_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate(self.process)
        finally:
            _remove_runtime_directory(self.runtime_directory)


def _configuration_payload(
    modules: Mapping[str, Mapping[str, Any]], startup_timeout: float
) -> bytes:
    """Return the configuration to send to the emitter, or refuse an invalid one."""
    if (
        not isinstance(startup_timeout, (int, float))
        or not math.isfinite(startup_timeout)
        or startup_timeout < 0
    ):
        startup_reason = "invalid_configuration"
        raise EmitterStartupError(startup_reason)
    if (
        not isinstance(modules, Mapping)
        or not modules
        or any(
            not isinstance(name, str) or not isinstance(settings, Mapping)
            for name, settings in modules.items()
        )
    ):
        startup_reason = "invalid_configuration"
        raise EmitterStartupError(startup_reason)
    payload = json.dumps(
        {name: dict(settings) for name, settings in modules.items()},
        allow_nan=False,
    ).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        startup_reason = "invalid_configuration"
        raise EmitterStartupError(startup_reason)
    return payload


def _send_configuration(writer: socket.socket, payload: bytes, deadline: float) -> None:
    """Write the configuration to the emitter's standard input before the deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        startup_reason = "configuration_timeout"
        raise EmitterStartupError(startup_reason)
    writer.settimeout(remaining)
    writer.sendall(payload)
    writer.shutdown(socket.SHUT_WR)
    if time.monotonic() >= deadline:
        startup_reason = "configuration_timeout"
        raise EmitterStartupError(startup_reason)


def _wait_for_readiness(
    process: subprocess.Popen, transport: SocketTransport, deadline: float
) -> None:
    """Return once the emitter answers a ping before the deadline."""
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            startup_reason = "child_exit"
            raise EmitterStartupError(startup_reason, exit_code=exit_code)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            startup_reason = "readiness_timeout"
            raise EmitterStartupError(startup_reason)
        try:
            transport.ping(timeout=min(transport.timeout, remaining))
        except LocalEmitterError:
            time.sleep(min(STARTUP_POLL_SECONDS, max(0, remaining)))
            continue
        break


def start_emitter(
    modules: Mapping[str, Mapping[str, Any]],
    *,
    command: str | os.PathLike[str] | None = None,
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> EmitterProcess:
    """Return after readiness and a protocol check, or clean up and raise."""
    directory: Path | None = None
    process: subprocess.Popen | None = None
    phase: StartupFailureReason = "invalid_configuration"
    try:
        payload = _configuration_payload(modules, startup_timeout)
        executable = resolve_command(command)
        deadline = time.monotonic() + startup_timeout
        phase = "startup_failure"
        # Short paths are necessary for macOS's Unix-socket path limit.
        directory = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX, dir="/tmp"))
        directory.chmod(0o700)
        socket_path = directory / SOCKET_FILENAME
        reader, writer = socket.socketpair()
        with reader, writer:
            # No shell: the program is the emitter command resolve_command found
            # with shutil.which, and the other arguments are fixed apart from
            # this run's socket path and this process's ID.
            process = subprocess.Popen(  # noqa: S603
                [
                    executable,
                    "host",
                    "--socket",
                    str(socket_path),
                    "--parent-pid",
                    str(os.getpid()),
                    "--skip-unavailable-modules",
                ],
                stdin=reader,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
            phase = "configuration_timeout"
            _send_configuration(writer, payload, deadline)
        phase = "startup_failure"
        transport = SocketTransport(socket_path)
        _wait_for_readiness(process, transport, deadline)
        description = _accepted_description(transport, deadline)
        return EmitterProcess(process, transport, directory, description)
    # Any failure stops the child, removes its directory, and is raised as a
    # startup error with a fixed reason, never as the original exception.
    except Exception as error:  # noqa: BLE001
        # Only transfer timeouts are classified as such; an unrelated writer
        # failure is a startup failure rather than evidence of deadline expiry.
        if isinstance(error, EmitterStartupError):
            failure = EmitterStartupError(error.reason, exit_code=error.exit_code)
        elif phase == "configuration_timeout" and isinstance(error, TimeoutError):
            failure = EmitterStartupError("configuration_timeout")
        else:
            failure = EmitterStartupError(
                phase if phase != "configuration_timeout" else "startup_failure"
            )
        try:
            try:
                if process is not None:
                    _terminate(process)
            finally:
                if directory is not None:
                    _remove_runtime_directory(directory)
        # A failure while cleaning up is raised as a startup error with a fixed
        # reason, never as the original exception.
        except Exception:  # noqa: BLE001
            failure = EmitterStartupError("startup_failure")
    # Raise outside the handler so neither a cause nor an implicit context
    # can carry private configuration or exception details.
    raise failure from None


def _accepted_description(
    transport: SocketTransport, deadline: float
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        startup_reason = "readiness_timeout"
        raise EmitterStartupError(startup_reason)
    try:
        description = transport.hello(timeout=remaining)
    except LocalEmitterError:
        # An emitter without a ``hello`` answer predates this protocol.
        startup_reason = "unsupported_protocol_version"
        raise EmitterStartupError(startup_reason) from None
    versions = description.get("protocol_versions")
    modules = description.get("modules")
    # Every call reads the module names as a set of strings, so a later
    # emitter's differently shaped answer is refused here, at startup.
    if (
        not isinstance(versions, list)
        or not all(type(version) is int for version in versions)
        or PROTOCOL_VERSION not in versions
        or not isinstance(modules, list)
        or not all(isinstance(module, str) for module in modules)
    ):
        startup_reason = "unsupported_protocol_version"
        raise EmitterStartupError(startup_reason)
    return description


def _terminate(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)


def _remove_runtime_directory(directory: Path) -> None:
    (directory / SOCKET_FILENAME).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except FileNotFoundError:
        # The child removes its own directory on a normal or failed exit.
        pass
    except OSError:
        pass

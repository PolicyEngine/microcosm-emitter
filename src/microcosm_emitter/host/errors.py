"""Explicit failures; callers decide whether their work can continue."""

from typing import Literal


class LocalServiceError(RuntimeError):
    """A local service could not start, accept a message, or shut down."""


class ModuleNotInstalledError(LocalServiceError):
    """No installed entry point provides the requested module."""


StartupFailureReason = Literal[
    "readiness_timeout",
    "configuration_timeout",
    "child_exit",
    "invalid_configuration",
    "startup_failure",
]

_STARTUP_MESSAGES = {
    "readiness_timeout": "Service readiness timed out",
    "configuration_timeout": "Service configuration transfer timed out",
    "child_exit": "Service exited before readiness",
    "invalid_configuration": "Service configuration is invalid or too large",
    "startup_failure": "Service startup failed",
}


class ServiceStartupError(LocalServiceError):
    """Fixed startup reason and optional exit code, never exception details."""

    def __init__(
        self,
        reason: StartupFailureReason,
        *,
        exit_code: int | None = None,
    ) -> None:
        """Hold ``reason`` and ``exit_code``, and build the fixed message from them.

        Raises ``ValueError`` for an unknown ``reason``. An ``exit_code`` whose type
        is not ``int`` is dropped.
        """
        if reason not in _STARTUP_MESSAGES:
            error_message = "Unknown startup failure reason."
            raise ValueError(error_message)
        self.reason = reason
        self.exit_code = exit_code if type(exit_code) is int else None
        metadata = [reason]
        if self.exit_code is not None:
            metadata.append(f"exit_code={self.exit_code}")
        super().__init__(f"{_STARTUP_MESSAGES[reason]} ({', '.join(metadata)}).")


def safe_startup_error(
    error: Exception, *, reason: StartupFailureReason = "startup_failure"
) -> ServiceStartupError:
    """Copy only safe metadata, without the original traceback, cause or context."""
    if isinstance(error, ServiceStartupError):
        return ServiceStartupError(error.reason, exit_code=error.exit_code)
    return ServiceStartupError(reason)

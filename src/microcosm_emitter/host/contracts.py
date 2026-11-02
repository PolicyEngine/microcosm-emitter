"""Interfaces shared by a local service host and its selected module."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

type JsonObject = Mapping[str, Any]


@dataclass(frozen=True)
class ModuleContext:
    """Identity of the process whose lifetime owns this service."""

    parent_pid: int
    services: dict[str, Any] = field(default_factory=dict)


class ServiceModule(Protocol):
    """Message/query callbacks run serially per module, independently of periodic work.

    Implementations must synchronize shared state. Returning from handle_message
    authorizes a success acknowledgement; persistence must finish before returning.
    Parent-exit and close callbacks run after accepted requests finish and periodic
    work stops. Close receives a monotonic deadline.
    """

    def initialize(self) -> None:
        """Prepare the module; the host calls this before it accepts requests."""
        ...

    def handle_message(self, message: JsonObject) -> None:
        """Handle one message, returning only once it is persisted."""
        ...

    def tick(self, now: float) -> None:
        """Do periodic work; ``now`` is a ``time.monotonic()`` reading."""
        ...

    def parent_exited(self) -> None:
        """Handle the exit of the parent process; ``close`` follows."""
        ...

    def close(self, deadline: float) -> None:
        """Stop the module by the monotonic ``deadline``."""
        ...

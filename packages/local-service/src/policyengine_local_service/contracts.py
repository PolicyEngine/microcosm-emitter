"""Interfaces shared by a local service host and its selected module."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

type JsonObject = Mapping[str, Any]


@dataclass(frozen=True)
class ModuleContext:
    """Identity of the process whose lifetime owns this service."""

    parent_pid: int


class ServiceModule(Protocol):
    """Message callbacks run on the socket thread; periodic work runs separately.

    Implementations must synchronize shared state. Returning from handle_message
    authorizes a success acknowledgement; persistence must finish before returning.
    close runs on the worker after periodic work stops, with a monotonic deadline.
    """

    def initialize(self) -> None: ...

    def handle_message(self, message: JsonObject) -> None: ...

    def tick(self, now: float) -> None: ...

    def parent_exited(self) -> None: ...

    def close(self, deadline: float) -> None: ...

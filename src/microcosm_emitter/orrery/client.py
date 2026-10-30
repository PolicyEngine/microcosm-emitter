"""Thin graph requests; hashing, persistence, auth and uploads stay in the service.

The messages, receipts and spool location come from
``microcosm_emitter_interface``, so this in-process client and the interface
package send the same requests. Each message is built inside the error
handling: no call raises, and an argument that cannot be sent gives an
unavailable receipt with the error code ``invalid_arguments``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from microcosm_emitter.host.constants import STARTUP_TIMEOUT
from microcosm_emitter.host.errors import (
    LocalServiceError,
    ServiceStartupError,
    safe_startup_error,
)
from microcosm_emitter.host.launcher import ServiceHandle, start_service
from microcosm_emitter.orrery.constants import GRAPH_REQUEST_TIMEOUT_SECONDS
from microcosm_emitter_interface.orrery import (
    answered_inventory,
    answered_receipt,
    enqueue_message,
    inventory_message,
    publication_id_of,
    receipt_message,
    retry_message,
    unavailable_receipt,
)
from microcosm_emitter_interface.spool import (
    default_spool_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from microcosm_emitter.host.contracts import JsonObject
    from microcosm_emitter.orrery.contracts import (
        PublicationFileRole,
        PublicationInventory,
        PublicationReceipt,
    )

# Every public name, including those re-exported from the interface package,
# so type checkers of this py.typed package treat them as exported.
__all__ = [
    "INVALID_ARGUMENTS",
    "UNAVAILABLE",
    "GraphPublicationClient",
    "GraphTransport",
    "default_spool_path",
    "publication_inventory",
    "publish_graph",
]

INVALID_ARGUMENTS = "invalid_arguments"
UNAVAILABLE = "local_service_unavailable"


class GraphTransport(Protocol):
    """Only a request adapter, never a database or HTTP implementation."""

    def request(
        self, message: JsonObject, *, timeout: float | None = None
    ) -> JsonObject:
        """Send ``message`` to the Orrery module and return its answer."""

    def close(self) -> None:
        """Close the transport.

        ``GraphPublicationClient.close`` calls it only for a transport passed
        in, not for one from ``start``.
        """


class GraphPublicationClient:
    """Send metadata to a separately running graph publisher over its private socket."""

    def __init__(self, transport: GraphTransport | None = None) -> None:
        """Hold ``transport``; without one, no request reaches the service."""
        self.transport = transport
        self._handle: ServiceHandle | None = None
        self.startup_error: ServiceStartupError | None = None

    @property
    def available(self) -> bool:
        """Whether a transport is set, so requests can reach the service."""
        return self.transport is not None

    @classmethod
    def start(
        cls,
        *,
        spool_path: Path | None = None,
        development_collector_url: str | None = None,
        publication_origin: str | None = None,
        startup_timeout: float = STARTUP_TIMEOUT,
    ) -> GraphPublicationClient:
        """Start the ``orrery`` module in an emitter host and return a client.

        The host is a child process configured with the event queue file
        (``spool_path``, or the default one) and the origins given. If it does
        not start, the client has no transport and ``startup_error`` says why;
        nothing is raised.
        """
        client = cls()
        try:
            configuration = {"spool_path": str(spool_path or default_spool_path())}
            if development_collector_url is not None:
                configuration["development_collector_url"] = development_collector_url
            if publication_origin is not None:
                configuration["publication_origin"] = publication_origin
            client._handle = start_service(
                "orrery", configuration, startup_timeout=startup_timeout
            )
            client.transport = client._handle.client.for_module("orrery")
        except (LocalServiceError, OSError, ValueError, TypeError) as error:
            # No acknowledgement means no claim that a durable job exists.
            client.startup_error = safe_startup_error(error)
        return client

    def _request(
        self, build_message: Callable[[], JsonObject]
    ) -> tuple[JsonObject | None, str]:
        """Return the module's answer, or ``None``, and a receipt error code.

        The answer is ``None`` without a transport or when the request fails,
        with ``UNAVAILABLE``, and when ``build_message`` raises, with
        ``INVALID_ARGUMENTS``. An answer comes with ``UNAVAILABLE``, the code a
        receipt reports if the answer holds no receipt.
        """
        if self.transport is None:
            return None, UNAVAILABLE
        try:
            message = dict(build_message())
        except Exception:
            return None, INVALID_ARGUMENTS
        try:
            return (
                self.transport.request(message, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS),
                UNAVAILABLE,
            )
        except Exception:
            return None, UNAVAILABLE

    def _receipt(
        self, build_message: Callable[[], JsonObject], publication_id: str | None
    ) -> PublicationReceipt:
        answer, error_code = self._request(build_message)
        return answered_receipt(answer, publication_id, error_code=error_code)

    def inventory(
        self,
        directory: Path,
        roles: Mapping[str, PublicationFileRole],
        *,
        publication_id: str | None = None,
    ) -> PublicationInventory | None:
        """Ask the service to hash the files ``roles`` names in ``directory``.

        The result is the service's inventory, or ``None`` when there is no
        answer or it holds none.
        """
        answer, _ = self._request(
            lambda: inventory_message(directory, roles, publication_id)
        )
        return answered_inventory(answer)

    def enqueue(
        self, directory: Path, inventory: PublicationInventory
    ) -> PublicationReceipt:
        """Ask the service to queue the graph in ``directory``; return its receipt.

        An inventory without a string ``publication_id`` is not sent; it gets an
        unavailable receipt with ``invalid_arguments``.
        """
        publication_id = publication_id_of(inventory)
        if publication_id is None:
            return unavailable_receipt(None, error_code=INVALID_ARGUMENTS)
        return self._receipt(
            lambda: enqueue_message(directory, inventory), publication_id
        )

    def receipt(self, publication_id: str) -> PublicationReceipt:
        """Ask the service for the receipt of ``publication_id``."""
        return self._receipt(lambda: receipt_message(publication_id), publication_id)

    def retry(
        self, publication_id: str, *, wait_seconds: float = 0
    ) -> PublicationReceipt:
        """Ask the service to retry a publication, then wait for its next attempt.

        The receipt is read again every 0.1 seconds, for up to ``wait_seconds``,
        while it is ``pending`` with the same number of attempts; the last
        receipt read is returned.
        """
        receipt = self._receipt(lambda: retry_message(publication_id), publication_id)
        previous_attempts = receipt.get("attempts", 0)
        deadline = time.monotonic() + max(0, wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and receipt.get("attempts", 0) == previous_attempts
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.receipt(publication_id)
        return receipt

    def close(self) -> None:
        """Shut down the started host, or close the given transport, then drop it.

        The host's shutdown is requested without waiting for it to exit.
        ``LocalServiceError`` and ``OSError`` are ignored; afterwards
        ``available`` is false.
        """
        try:
            if self._handle is not None:
                self._handle.close()
            elif self.transport is not None:
                self.transport.close()
        except (LocalServiceError, OSError):
            pass
        finally:
            self.transport = None


def publication_inventory(
    directory: Path,
    roles: Mapping[str, PublicationFileRole],
    *,
    publication_id: str | None = None,
    client: GraphPublicationClient | None = None,
    spool_path: Path | None = None,
) -> PublicationInventory | None:
    """Ask the service to hash allowed files without importing its implementation."""
    publisher = (
        client
        if client is not None
        else GraphPublicationClient.start(spool_path=spool_path)
    )
    try:
        return publisher.inventory(directory, roles, publication_id=publication_id)
    finally:
        if client is None:
            publisher.close()


def publish_graph(
    directory: Path,
    inventory: PublicationInventory,
    *,
    spool_path: Path | None = None,
    client: GraphPublicationClient | None = None,
    wait_seconds: float = 30.0,
) -> PublicationReceipt:
    """Wait only after service-side durable enqueue; never write a fallback queue."""
    publisher = (
        client
        if client is not None
        else GraphPublicationClient.start(spool_path=spool_path)
    )
    try:
        receipt = publisher.enqueue(directory, inventory)
        deadline = time.monotonic() + max(0, wait_seconds)
        while (
            publisher.available
            and receipt["status"] == "pending"
            and not receipt.get("attempts", 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = publisher.receipt(inventory["publication_id"])
        return receipt
    finally:
        if client is None:
            publisher.close()

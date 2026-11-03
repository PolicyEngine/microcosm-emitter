"""Completed graph delivery, independent of telemetry registration and retention."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from microcosm_emitter.core.auth import shared_session
from microcosm_emitter.core.collector_origin import configured_collector_origin
from microcosm_emitter.host.contracts import JsonObject, ModuleContext
from microcosm_emitter.orrery.service.queue import (
    DEFAULT_GRAPH_PUBLICATION_ORIGIN,
    GRAPH_WORKER_ERROR_MESSAGE,
    GraphPublicationDelivery,
    GraphPublicationQueue,
    publication_inventory,
)

if TYPE_CHECKING:
    from microcosm_emitter.orrery.contracts import PublicationInventory

# Each print in this module that suppresses Ruff's T201 writes one of its
# messages to standard error on purpose; none is leftover debugging output.


class OrreryModule:
    """Queue graph publications on request and deliver them on each tick."""

    def __init__(self, configuration: JsonObject, context: ModuleContext) -> None:
        """Hold the configuration and context; ``initialize`` opens the queue."""
        self.configuration, self.context = configuration, context
        self.queue: GraphPublicationQueue | None = None
        self.delivery: GraphPublicationDelivery | None = None
        self._warned = False

    def initialize(self) -> None:
        """Open the graph publication queue and set up its delivery.

        An untrusted collector origin, or an unusable collector token, raises
        an error before the queue opens. The queue is kept in the event queue
        file the ``spool_path`` setting names, and the credential is sent only
        to the publication origin.
        """
        # Refuse an untrusted collector origin, or an unusable given
        # collector token, before opening the queue.
        origin = configured_collector_origin(self.configuration)
        publication_origin = self.configuration.get(
            "publication_origin", DEFAULT_GRAPH_PUBLICATION_ORIGIN
        )
        # The credential goes to the publication origin, not the collector,
        # so a collector token given to this emitter is never sent there.
        session = shared_session(
            self.context.services, origin, destination=publication_origin
        )
        queue = GraphPublicationQueue(Path(self.configuration["spool_path"]))
        self.queue = queue
        self.delivery = GraphPublicationDelivery(
            queue,
            credential=session.credential,
            invalidate_credential=session.invalidate,
            origin=publication_origin,
        )

    def handle_message(self, message: JsonObject) -> None:
        """Queue the graph in ``message["directory"]`` with the message's inventory.

        ``GraphPublicationQueue.enqueue`` hashes the files again and raises
        ``ValueError`` if they differ from the inventory.
        """
        if self.queue is None:
            error_message = "The host initializes modules first."
            raise AssertionError(error_message)
        # enqueue() recomputes the inventory from the files and rejects any
        # message whose inventory differs, so this boundary is validated.
        inventory = cast("PublicationInventory", dict(message["inventory"]))
        self.queue.enqueue(Path(message["directory"]), inventory)

    def query(self, message: JsonObject) -> dict:
        """Construct/persist results before the host returns its JSON response."""
        if self.queue is None:
            error_message = "The host initializes modules first."
            raise AssertionError(error_message)
        command = message.get("command")
        if command == "inventory":
            return {
                "inventory": publication_inventory(
                    Path(message["directory"]),
                    message["roles"],
                    publication_id=message.get("publication_id"),
                )
            }
        if command == "enqueue":
            self.handle_message(message)
            return {
                "receipt": self.queue.receipt(message["inventory"]["publication_id"])
            }
        if command == "retry":
            self.queue.retry(message["publication_id"])
        elif command != "receipt":
            error_message = "unsupported graph publication command"
            raise ValueError(error_message)
        return {"receipt": self.queue.receipt(message["publication_id"])}

    def tick(self, now: float) -> None:
        """Try to deliver one queued graph publication; errors print a warning once.

        An exception from the attempt is not raised, since the host stops a
        module whose ``tick`` raises. ``now`` is not used.
        """
        if self.delivery is None:
            error_message = "The host initializes modules first."
            raise AssertionError(error_message)
        try:
            self.delivery.flush_once()
        # The host stops a module whose tick raises. A database or
        # filesystem error leaves the job leased for a later retry; it
        # must not end delivery of every other job.
        except Exception:  # noqa: BLE001
            if not self._warned:
                print(GRAPH_WORKER_ERROR_MESSAGE, file=sys.stderr, flush=True)  # noqa: T201
                self._warned = True

    def parent_exited(self) -> None:
        """Preserved jobs remain eligible when a later host resumes delivery."""

    def close(self, deadline: float) -> None:
        """Close the delivery's HTTP client and the queue's database connections.

        ``deadline`` is not used.
        """
        if self.delivery is not None:
            self.delivery.client.close()
        if self.queue is not None:
            self.queue.close()

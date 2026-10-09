"""Completed graph delivery, independent of telemetry registration and retention."""

import sys
from pathlib import Path
from typing import cast

from microcosm_provider_client.contracts import JsonObject, ModuleContext
from microcosm_provider_core.auth import (
    _collector_origin,
    _development_collector_url,
    shared_session,
)
from microcosm_provider_core.constants import PRODUCTION_COLLECTOR_URL

from microcosm_provider_orrery.contracts import PublicationInventory
from microcosm_provider_orrery.service.queue import (
    DEFAULT_GRAPH_PUBLICATION_ORIGIN,
    GRAPH_WORKER_ERROR_MESSAGE,
    GraphPublicationDelivery,
    GraphPublicationQueue,
)


class OrreryModule:
    def __init__(self, configuration: JsonObject, context: ModuleContext):
        self.configuration, self.context = configuration, context
        self.queue: GraphPublicationQueue | None = None
        self.delivery: GraphPublicationDelivery | None = None
        self._warned = False

    def initialize(self) -> None:
        queue = GraphPublicationQueue(Path(self.configuration["spool_path"]))
        self.queue = queue
        origin = (
            _development_collector_url(self.configuration["development_collector_url"])
            if self.configuration.get("development_collector_url")
            else _collector_origin(PRODUCTION_COLLECTOR_URL)
        )
        session = shared_session(self.context.services, origin)
        self.delivery = GraphPublicationDelivery(
            queue,
            credential=session.credential,
            invalidate_credential=session.invalidate,
            origin=self.configuration.get(
                "publication_origin", DEFAULT_GRAPH_PUBLICATION_ORIGIN
            ),
        )

    def handle_message(self, message: JsonObject) -> None:
        assert self.queue is not None, "The host initializes modules first."
        # enqueue() recomputes the inventory from the files and rejects any
        # message whose inventory differs, so this boundary is validated.
        inventory = cast(PublicationInventory, dict(message["inventory"]))
        self.queue.enqueue(Path(message["directory"]), inventory)

    def tick(self, now: float) -> None:
        assert self.delivery is not None, "The host initializes modules first."
        try:
            self.delivery.flush_once()
        except Exception:
            # The host stops a module whose tick raises. A database or
            # filesystem error leaves the job leased for a later retry; it
            # must not end delivery of every other job.
            if not self._warned:
                print(GRAPH_WORKER_ERROR_MESSAGE, file=sys.stderr, flush=True)
                self._warned = True

    def parent_exited(self) -> None:
        """Preserved jobs remain eligible when a later host resumes delivery."""

    def close(self, deadline: float) -> None:
        if self.delivery is not None:
            self.delivery.client.close()
        if self.queue is not None:
            self.queue.close()

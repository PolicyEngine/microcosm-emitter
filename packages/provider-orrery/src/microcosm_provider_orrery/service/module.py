"""Completed graph delivery, independent of telemetry registration and retention."""

from pathlib import Path

from microcosm_provider_client.contracts import JsonObject, ModuleContext
from microcosm_provider_core.auth import (
    _collector_origin,
    _development_collector_url,
    shared_session,
)
from microcosm_provider_core.constants import PRODUCTION_COLLECTOR_URL

from microcosm_provider_orrery.service.queue import (
    DEFAULT_GRAPH_PUBLICATION_ORIGIN,
    GraphPublicationDelivery,
    GraphPublicationQueue,
)


class OrreryModule:
    def __init__(self, configuration: JsonObject, context: ModuleContext):
        self.configuration, self.context = configuration, context
        self.queue = self.delivery = None

    def initialize(self) -> None:
        self.queue = GraphPublicationQueue(Path(self.configuration["spool_path"]))
        origin = (
            _development_collector_url(self.configuration["development_collector_url"])
            if self.configuration.get("development_collector_url")
            else _collector_origin(PRODUCTION_COLLECTOR_URL)
        )
        session = shared_session(self.context.services, origin)
        self.delivery = GraphPublicationDelivery(
            self.queue,
            credential=session.credential,
            invalidate_credential=session.invalidate,
            origin=self.configuration.get(
                "publication_origin", DEFAULT_GRAPH_PUBLICATION_ORIGIN
            ),
        )

    def handle_message(self, message: JsonObject) -> None:
        self.queue.enqueue(Path(message["directory"]), dict(message["inventory"]))

    def tick(self, now: float) -> None:
        self.delivery.flush_once()

    def parent_exited(self) -> None:
        """Preserved jobs remain eligible when a later host resumes delivery."""

    def close(self, deadline: float) -> None:
        if self.delivery is not None:
            self.delivery.client.close()
        if self.queue is not None:
            self.queue.close()

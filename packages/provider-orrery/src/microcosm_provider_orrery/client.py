"""Build-facing durable enqueue; all authentication and HTTP run in the service."""

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from microcosm_provider_orrery.contracts import (
    PublicationFileRole,
    PublicationInventory,
    PublicationReceipt,
)


def default_spool_path() -> Path:
    """Keep Microcosm's existing cache path so queued jobs survive migration."""
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"


def publication_inventory(
    directory: Path,
    roles: Mapping[str, PublicationFileRole],
    *,
    publication_id: str | None = None,
) -> PublicationInventory:
    """Hash an explicit set of public-safe graph files without network access."""
    from microcosm_provider_orrery.service.queue import (
        publication_inventory as inventory,
    )

    return inventory(directory, roles, publication_id=publication_id)


def publish_graph(
    directory: Path,
    inventory: PublicationInventory,
    *,
    spool_path: Path | None = None,
    available: Callable[[], bool] = lambda: False,
    wait_seconds: float = 30.0,
) -> PublicationReceipt:
    """Persist before waiting; unavailable services never lose a publication job."""
    from microcosm_provider_orrery.service.queue import GraphPublicationQueue

    queue = GraphPublicationQueue(spool_path or default_spool_path())
    try:
        queue.enqueue(directory, inventory)
        deadline = time.monotonic() + max(0, wait_seconds)
        while available() and time.monotonic() < deadline:
            receipt = queue.receipt(inventory["publication_id"])
            assert receipt is not None, (
                "A queued graph publication must have a receipt."
            )
            if receipt.get("attempts", 0) or receipt["status"] == "published":
                return receipt
            time.sleep(0.1)
        receipt = queue.receipt(inventory["publication_id"])
        assert receipt is not None, "A queued graph publication must have a receipt."
        return receipt
    finally:
        queue.close()

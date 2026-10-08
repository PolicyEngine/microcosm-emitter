"""Public enqueue remains durable when no service or telemetry run exists."""

import json

from microcosm_provider_core.auth import shared_session
from microcosm_provider_orrery.client import publication_inventory, publish_graph
from microcosm_provider_orrery.service.queue import GraphPublicationQueue


def test_public_client_persists_without_service_or_run(tmp_path):
    directory = tmp_path / "graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text(json.dumps({"nodes": []}))
    inventory = publication_inventory(
        directory, {"graph.orrery.json": "graph"}, publication_id="independent-graph"
    )
    path = tmp_path / "jobs.sqlite3"
    receipt = publish_graph(directory, inventory, spool_path=path, wait_seconds=0)
    assert receipt["status"] == "pending"
    assert receipt["publication_id"] == "independent-graph"
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt("independent-graph") == receipt
    finally:
        queue.close()


def test_modules_share_one_session_and_reject_conflicting_origins():
    import pytest

    services = {}
    first = shared_session(services, "https://collector.example")
    assert shared_session(services, "https://collector.example") is first
    with pytest.raises(ValueError, match="same collector origin"):
        shared_session(services, "https://different.example")

"""Runs in an isolated interpreter containing only the installed distributions."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def base_smoke():
    from microcosm_provider_client.client import SocketClient
    from microcosm_provider_client.launcher import start_service

    assert SocketClient and start_service
    for name in (
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
        "microcosm_provider_telemetry",
        "microcosm",
    ):
        assert find_spec(name) is None, name
    assert distribution("microcosm-provider-client").version


def telemetry_smoke():
    from microcosm_provider_telemetry.client import LocalTelemetryEmitter, TelemetryRun

    assert "sqlalchemy" not in sys.modules
    assert "huggingface_hub" not in sys.modules
    assert find_spec("microcosm") is None
    run = TelemetryRun("wheel-test", "UK", "installed")
    path = Path.cwd() / "spool" / "events.sqlite3"
    emitter = LocalTelemetryEmitter.start(
        run=run, spool_path=path, development_collector_url="http://127.0.0.1:1"
    )
    assert emitter.available
    try:
        emitter.stage("compile")
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    from microcosm_provider_core.migrations import (
        current_database_revision,
    )
    from microcosm_provider_telemetry.service.spool import EventSpool

    assert current_database_revision(path) == "20261008_02"
    spool = EventSpool(path)
    try:
        assert not spool.has_deliverable()
        assert [
            event["sequence"] for event in spool.batch(run.run_id, run.producer_id)
        ] == [1, 2, 3]
    finally:
        spool.close()


def combined_smoke():
    from microcosm_provider_client.launcher import start_services
    from microcosm_provider_orrery.client import publication_inventory, publish_graph
    from microcosm_provider_orrery.service.queue import GraphPublicationQueue
    from microcosm_provider_telemetry.client import LocalTelemetryEmitter, TelemetryRun

    path = Path.cwd() / "combined.sqlite3"
    run = TelemetryRun("combined-test", "GB", "installed")
    handle = start_services(
        {
            "telemetry": {
                "registration": run.as_registration(),
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
            },
            "orrery": {
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
            },
        }
    )
    directory = Path.cwd() / "graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    inventory = publication_inventory(directory, {"graph.orrery.json": "graph"})
    try:
        emitter = LocalTelemetryEmitter(
            run=run, transport=handle.client.for_module("telemetry")
        )
        emitter.stage("compile")
        emitter.complete()
        handle.client.ping()  # Completing telemetry must not close publication.
        receipt = publish_graph(directory, inventory, spool_path=path, wait_seconds=0)
        assert receipt["status"] == "pending" and "run_id" not in inventory
        handle.client.for_module("orrery").send(
            {"directory": str(directory), "inventory": inventory}
        )
    finally:
        handle.close()
        assert handle.process.wait(timeout=20) == 0
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt(inventory["publication_id"])["status"] == "pending"
    finally:
        queue.close()


if __name__ == "__main__":
    {"base": base_smoke, "telemetry": telemetry_smoke, "combined": combined_smoke}[
        sys.argv[1]
    ]()

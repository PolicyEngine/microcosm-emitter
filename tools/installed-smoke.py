"""Verify the single installed distribution away from the source checkout."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def host_smoke() -> None:
    """Check the host's imports and the installed distribution's metadata.

    Microcosm must not be installed; SQLAlchemy, Alembic and huggingface_hub must
    be installed but not imported by the host; importing the host must not import
    the telemetry package, and importing the Orrery client must not import the
    core or Orrery service packages.
    """
    from microcosm_emitter.host.client import SocketClient
    from microcosm_emitter.host.launcher import start_service

    assert SocketClient
    assert start_service
    assert find_spec("microcosm") is None
    emitter = distribution("microcosm-emitter")
    assert emitter.version
    assert emitter.metadata.get_all("Project-URL") == [
        "Source, https://github.com/PolicyEngine/microcosm-emitter"
    ]
    for name in ("sqlalchemy", "alembic", "huggingface_hub"):
        assert find_spec(name) is not None, name
        assert name not in sys.modules, name
    assert "microcosm_emitter.telemetry" not in sys.modules
    from microcosm_emitter.orrery.client import GraphPublicationClient

    assert GraphPublicationClient
    assert "microcosm_emitter.core" not in sys.modules
    assert "microcosm_emitter.orrery.service" not in sys.modules


def telemetry_smoke() -> None:
    """Run a telemetry emitter, then check the event queue database it leaves.

    Importing the telemetry client must not import SQLAlchemy or huggingface_hub.
    After a stage and completion, the service process must exit with 0, the
    database must be at revision 20261008_02, no queued event may still be
    eligible for delivery, and the run's events must have sequences 1 to 3.
    """
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    assert "sqlalchemy" not in sys.modules
    assert "huggingface_hub" not in sys.modules
    assert find_spec("microcosm") is None
    run = TelemetryRun("wheel-test", "UK", "installed")
    path = Path.cwd() / "spool" / "events.sqlite3"
    emitter = LocalTelemetryEmitter.start(
        run=run,
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        # A clean wheel install must compile dependency bytecode on first use.
        # Test packaging independently of the production startup-time limit.
        startup_timeout_seconds=10,
    )
    assert emitter.available
    try:
        emitter.stage("compile")
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    from microcosm_emitter.core.migrations import (
        current_database_revision,
    )
    from microcosm_emitter.telemetry.service.spool import EventSpool

    assert current_database_revision(path) == "20261008_02"
    spool = EventSpool(path)
    try:
        assert not spool.has_deliverable()
        assert [
            event["sequence"] for event in spool.batch(run.run_id, run.producer_id)
        ] == [1, 2, 3]
    finally:
        spool.close()


def graph_only_smoke() -> None:
    """Publish a graph through a service that runs only the Orrery module.

    The inventory must have no run ID, the publication must be pending, the
    service process must exit with 0, and the publication queue must still hold
    the publication as pending.
    """
    from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
    from microcosm_emitter.orrery.service.queue import GraphPublicationQueue

    path = Path.cwd() / "graph-only.sqlite3"
    directory = Path.cwd() / "graph-only"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    client = GraphPublicationClient.start(
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
        startup_timeout=10,
    )
    assert client.available
    try:
        inventory = client.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        assert "run_id" not in inventory
        receipt = publish_graph(directory, inventory, client=client, wait_seconds=0)
        assert receipt["status"] == "pending"
    finally:
        handle = client._handle
        client.close()
        assert handle.process.wait(timeout=20) == 0
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt(inventory["publication_id"])["status"] == "pending"
    finally:
        queue.close()


def combined_smoke() -> None:
    """Run the telemetry and Orrery modules in one service sharing one database.

    After a stage, a blocked status and a graph publication, the service process
    must exit with 0 and the run's last queued event must be the blocked one.
    """
    from microcosm_emitter.host.launcher import start_services
    from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    path = Path.cwd() / "combined.sqlite3"
    run = TelemetryRun("combined-test", "UK", "installed")
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
                "publication_origin": "http://127.0.0.1:1",
            },
        },
        startup_timeout=10,
    )
    client = GraphPublicationClient(handle.client.for_module("orrery"))
    directory = Path.cwd() / "combined-graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    try:
        emitter = LocalTelemetryEmitter(
            run=run,
            transport=handle.client.for_module("telemetry"),
            handle=handle,
            close_service_on_finish=False,
        )
        emitter.stage("hf_download")
        emitter.block(phase="preflight", blocking_gate_ids=["check"])
        handle.client.ping()
        inventory = client.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        assert (
            publish_graph(directory, inventory, client=client, wait_seconds=0)["status"]
            == "pending"
        )
    finally:
        client.close()
        emitter.close()
        assert handle.process.wait(timeout=20) == 0
    from microcosm_emitter.telemetry.service.spool import EventSpool

    spool = EventSpool(path)
    try:
        assert spool.batch(run.run_id, run.producer_id)[-1]["status"] == "blocked"
    finally:
        spool.close()


def coded_failure_smoke() -> None:
    """Record a failure with an error code, then check the events it leaves.

    The service process must exit with 0, and the last two events, the failed
    stage and the run's failure, must each carry ``BUILD_OUT_OF_MEMORY``.
    """
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    run = TelemetryRun("failure-test", "UK", "installed")
    path = Path.cwd() / "failure.sqlite3"
    emitter = LocalTelemetryEmitter.start(
        run=run,
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        startup_timeout_seconds=10,
    )
    assert emitter.available
    try:
        emitter.transition_stage("hf_download")
        emitter.fail(
            MemoryError("synthetic"),
            failed_during="hf_download",
            failure_class="oom",
            error_code="BUILD_OUT_OF_MEMORY",
        )
        assert emitter._handle.process.wait(timeout=20) == 0
    finally:
        emitter.close()
    from microcosm_emitter.telemetry.service.spool import EventSpool

    spool = EventSpool(path)
    try:
        events = spool.batch(run.run_id, run.producer_id)
        assert [(event["stage_id"], event["status"]) for event in events[-2:]] == [
            ("hf_download", "failed"),
            ("failed", "failed"),
        ]
        assert all(
            event["details"]["error_code"] == "BUILD_OUT_OF_MEMORY"
            for event in events[-2:]
        )
    finally:
        spool.close()


if __name__ == "__main__":
    host_smoke()
    telemetry_smoke()
    graph_only_smoke()
    combined_smoke()
    coded_failure_smoke()

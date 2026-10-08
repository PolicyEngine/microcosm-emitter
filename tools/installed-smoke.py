"""Runs in an isolated interpreter containing only the installed distributions."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def base_smoke():
    from policyengine_local_service.client import SocketClient
    from policyengine_local_service.launcher import start_service

    assert SocketClient and start_service
    for name in (
        "sqlalchemy",
        "alembic",
        "huggingface_hub",
        "policyengine_telemetry",
        "microcosm",
    ):
        assert find_spec(name) is None, name
    assert distribution("policyengine-local-service").version


def telemetry_smoke():
    from policyengine_telemetry.client import LocalTelemetryEmitter, TelemetryRun

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
    from policyengine_telemetry.service.migrations import current_database_revision
    from policyengine_telemetry.service.spool import EventSpool

    assert current_database_revision(path) == "20261007_01"
    spool = EventSpool(path)
    try:
        assert not spool.has_deliverable()
        assert [
            event["sequence"] for event in spool.batch(run.run_id, run.producer_id)
        ] == [1, 2, 3]
    finally:
        spool.close()


if __name__ == "__main__":
    {"base": base_smoke, "telemetry": telemetry_smoke}[sys.argv[1]]()

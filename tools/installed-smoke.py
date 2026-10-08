"""Verify the single installed distribution away from the source checkout."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def host_smoke():
    from microcosm_emitter.host.client import SocketClient
    from microcosm_emitter.host.launcher import start_service

    assert SocketClient and start_service
    assert find_spec("microcosm") is None
    assert distribution("microcosm-emitter").version
    for name in ("sqlalchemy", "alembic", "huggingface_hub"):
        assert find_spec(name) is not None, name
        assert name not in sys.modules, name
    assert "microcosm_emitter.telemetry" not in sys.modules


def telemetry_smoke():
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
    from microcosm_emitter.telemetry.service.migrations import (
        current_database_revision,
    )
    from microcosm_emitter.telemetry.service.spool import EventSpool

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
    host_smoke()
    telemetry_smoke()

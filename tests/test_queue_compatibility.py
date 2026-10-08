"""Open a synthetic #1099 queue without reassigning IDs or upload eligibility."""

import hashlib
import json
import shutil
from pathlib import Path

from policyengine_telemetry.service import spool as spool_module
from policyengine_telemetry.service.database import create_spool_engine
from policyengine_telemetry.service.models import TelemetryEventRecord
from policyengine_telemetry.service.spool import EventSpool
from sqlalchemy import select
from sqlalchemy.orm import Session

FIXTURES = Path(__file__).parent / "fixtures"


def test_current_microcosm_queue_is_preserved(tmp_path, monkeypatch):
    source = FIXTURES / "microcosm-1099.sqlite3"
    provenance = json.loads((FIXTURES / "microcosm-1099.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance["sha256"]
    path = tmp_path / "events.sqlite3"
    shutil.copyfile(source, path)
    # Fixture timestamps are fixed; retention itself is covered separately.
    monkeypatch.setattr(spool_module, "RETENTION_DAYS", 100_000)
    engine = create_spool_engine(path)
    with Session(engine) as session:
        before = {
            event.event_id: event.payload
            for event in session.scalars(select(TelemetryEventRecord))
        }
    engine.dispose()
    spool = EventSpool(path)
    try:
        pending = spool.pending_runs()
        assert len(pending) == 1 and pending[0]["producer_id"] == "pending-producer"
        run_id = "microcosm-1099-fixture"
        events = spool.batch(run_id, "pending-producer") + spool.batch(
            run_id, "local-producer"
        )
        assert {event["event_id"]: event for event in events} == before
        assert [
            event["sequence"] for event in spool.batch(run_id, "pending-producer")
        ] == [2]
        event = spool.append(pending[0], {"event_type": "run", "status": "completed"})
        assert event["sequence"] == 3 and event["event_id"] not in before
        spool.acknowledge(
            [entry["event_id"] for entry in spool.batch(run_id, "pending-producer")]
        )
        assert not spool.has_deliverable() and spool.has_pending()
    finally:
        spool.close()

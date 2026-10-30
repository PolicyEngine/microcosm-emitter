"""Open older spool databases, and leave them readable by released emitters.

A synthetic #1099 queue opens without reassigning IDs or upload eligibility,
and hand-overs never move the shared spool database past a released revision.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.orm import Session

from microcosm_emitter.core import migrations as migrations_module
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.migrations import alembic_config, current_database_revision
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.telemetry.service import spool as spool_module
from microcosm_emitter.telemetry.service.spool import EventSpool

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
        assert len(pending) == 1
        assert pending[0]["producer_id"] == "pending-producer"
        run_id = "microcosm-1099-fixture"
        events = spool.batch(run_id, "pending-producer") + spool.batch(
            run_id, "local-producer"
        )
        assert {event["event_id"]: event for event in events} == before
        assert [
            event["sequence"] for event in spool.batch(run_id, "pending-producer")
        ] == [2]
        event = spool.append(pending[0], {"event_type": "run", "status": "completed"})
        assert event["sequence"] == 3
        assert event["event_id"] not in before
        spool.acknowledge(
            [entry["event_id"] for entry in spool.batch(run_id, "pending-producer")]
        )
        assert not spool.has_deliverable()
        assert spool.has_pending()
    finally:
        spool.close()


#: The spool migrations that released emitters (0.2.0 to 0.4.0) ship.
RELEASED_SPOOL_MIGRATIONS = (
    "v20261007_01_initial_spool.py",
    "v20261008_02_graph_publication.py",
)
RELEASED_SPOOL_REVISION = "20261008_02"


def released_spool_history(directory: Path) -> ScriptDirectory:
    """Build a migration history holding only the event queue's released migrations."""
    source = Path(migrations_module.__file__).parent / "alembic"
    (directory / "versions").mkdir(parents=True)
    shutil.copyfile(source / "script.py.mako", directory / "script.py.mako")
    for name in RELEASED_SPOOL_MIGRATIONS:
        shutil.copyfile(source / "versions" / name, directory / "versions" / name)
    return ScriptDirectory(str(directory))


def released_spool_database(path: Path) -> None:
    """Create an event queue database as a released emitter leaves it."""
    engine = create_spool_engine(path)
    try:
        with (
            engine.begin() as connection,
            alembic_config(connection=connection) as config,
        ):
            command.upgrade(config, RELEASED_SPOOL_REVISION)
    finally:
        engine.dispose()


def test_hand_overs_leave_the_shared_spool_database_at_a_released_revision(
    tmp_path,
):
    """Released emitters refuse a spool database at a revision they don't know.

    Microcosm builds pinned to a released emitter share this database with
    every newer emitter on the machine, so hand-overs must not migrate it.
    """
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule
    from microcosm_emitter_interface.staging import HuggingFaceSettings

    spool_path = tmp_path / "events.sqlite3"
    released_spool_database(spool_path)
    spool = EventSpool(spool_path)
    module = HuggingFaceModule(
        HuggingFaceSettings().configuration(spool_path),
        ModuleContext(os.getpid()),
        api_factory=lambda: None,
    )
    module.initialize()
    module.close(0)
    spool.close()

    revision = current_database_revision(spool_path)
    # A released emitter's check before it writes anything.
    released_spool_history(tmp_path / "released-alembic").get_revision(revision)
    assert revision == RELEASED_SPOOL_REVISION
    assert (tmp_path / "events.handovers.sqlite3").exists()

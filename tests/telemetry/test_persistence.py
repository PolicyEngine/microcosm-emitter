"""Persistence uses the packaged migration history and preserves event identity."""

import hashlib

import pytest
from alembic import command
from sqlalchemy import inspect

from microcosm_emitter.telemetry.service.database import create_spool_engine
from microcosm_emitter.telemetry.service.migrations import (
    alembic_config,
    current_database_revision,
)
from microcosm_emitter.telemetry.service.spool import EventSpool


def registration(producer="producer-a"):
    return dict(
        run_id="fixture-run",
        producer_id=producer,
        country_code="UK",
        pipeline="test",
        run_kind="build",
    )


def test_migrations_match_models_and_reopening_preserves_sequences(tmp_path):
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    spool.register(registration())
    first = spool.append(registration(), {"event_type": "run", "status": "started"})
    spool.close()
    spool = EventSpool(path)
    second = spool.append(registration(), {"event_type": "run", "status": "completed"})
    assert [
        event["sequence"] for event in spool.batch("fixture-run", "producer-a")
    ] == [1, 2]
    assert spool.batch("fixture-run", "producer-a")[0] == first
    assert first["event_id"] != second["event_id"]
    assert current_database_revision(path) == "20261007_01"
    with (
        spool._engine.begin() as connection,
        alembic_config(connection=connection) as config,
    ):
        command.check(config)
    spool.close()


def test_unversioned_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    # Simulate lost version metadata without handwritten SQL or schema adoption.
    spool.close()
    engine = create_spool_engine(path)
    from sqlalchemy import MetaData, Table

    version = Table("alembic_version", MetaData(), autoload_with=engine)
    version.drop(engine)
    engine.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="unversioned"):
        EventSpool(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_multiple_producers_and_local_only_survive_reopening(tmp_path):
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path)
    for producer in ("one", "two"):
        spool.register(registration(producer))
        spool.append(registration(producer), {"event_type": "run", "status": "started"})
    spool.make_local_only("fixture-run", "one", "missing_huggingface_credential")
    spool.close()
    spool = EventSpool(path)
    assert [run["producer_id"] for run in spool.pending_runs()] == ["two"]
    assert spool.batch("fixture-run", "one")[0]["sequence"] == 1
    two = spool.batch("fixture-run", "two")
    spool.acknowledge([event["event_id"] for event in two])
    assert not spool.has_deliverable()
    assert spool.has_pending()
    assert set(inspect(spool._engine).get_table_names()) == {
        "alembic_version",
        "telemetry_runs",
        "telemetry_events",
    }
    spool.close()

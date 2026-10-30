"""Programmatic Alembic migration entry points for the emitter's databases.

The emitter keeps two SQLite databases, each with its own migration history:

- the **spool database** (telemetry events and graph publication jobs). Every
  emitter on a machine shares it, including the released emitters that
  Microcosm builds pin, and those refuse a revision they don't know. Its
  history must stay at revisions that released emitters ship until they are
  no longer in use;
- the **hand-over database** of the Hugging Face module, next to the spool
  database. Only emitters with that module open it.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from microcosm_emitter.core.database import create_spool_engine

_MIGRATION_TARGET = "head"


@dataclass(frozen=True)
class MigrationHistory:
    """One database's migration scripts, in a directory of this package."""

    directory: str
    #: Names the database in the refusal of an unversioned file.
    label: str


SPOOL_HISTORY = MigrationHistory(directory="alembic", label="telemetry")
HANDOVER_HISTORY = MigrationHistory(directory="handover_alembic", label="hand-over")


@contextmanager
def alembic_config(
    *,
    connection: Connection | None = None,
    history: MigrationHistory = SPOOL_HISTORY,
) -> Iterator[Config]:
    """Yield Alembic configuration with ``history``'s migrations on the filesystem."""
    migration_resources = files(__package__).joinpath(history.directory)
    with as_file(migration_resources) as migration_directory:
        config = Config()
        config.set_main_option("script_location", str(migration_directory))
        if connection is not None:
            config.attributes["connection"] = connection
        yield config


def upgrade_database(engine: Engine, history: MigrationHistory) -> None:
    """Apply every committed migration of ``history`` to an engine."""
    # All build processes share these databases. Serialize migration checks and
    # upgrades; SQLite transaction locks alone do not serialize schema inspection.
    path = Path(engine.url.database)
    with path.with_suffix(".migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with engine.begin() as connection:
            names = set(inspect(connection).get_table_names())
            revision = MigrationContext.configure(connection).get_current_revision()
            if names and revision is None:
                error_message = (
                    f"Refusing an unversioned {history.label} database; "
                    "no changes made."
                )
                raise RuntimeError(error_message)
            with alembic_config(connection=connection, history=history) as config:
                scripts = ScriptDirectory.from_config(config)
                if revision is not None:
                    # Reject unknown revisions, including the other history's,
                    # before writes.
                    scripts.get_revision(revision)
                command.upgrade(config, _MIGRATION_TARGET)


def upgrade_spool_database(engine: Engine) -> None:
    """Apply every committed spool migration to an engine."""
    upgrade_database(engine, SPOOL_HISTORY)


def upgrade_handover_database(engine: Engine) -> None:
    """Apply every committed hand-over migration to an engine."""
    upgrade_database(engine, HANDOVER_HISTORY)


def current_database_revision(path: Path | str) -> str | None:
    """Return the Alembic revision recorded by one emitter database."""
    engine = create_spool_engine(path)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def migration_head_revision(history: MigrationHistory = SPOOL_HISTORY) -> str | None:
    """Return the single current head from one packaged migration history."""
    with alembic_config(history=history) as config:
        return ScriptDirectory.from_config(config).get_current_head()

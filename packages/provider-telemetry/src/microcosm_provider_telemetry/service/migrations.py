"""Programmatic Alembic migration entry points for the telemetry spool."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from microcosm_provider_telemetry.service.database import create_spool_engine

_MIGRATION_TARGET = "head"


@contextmanager
def alembic_config(
    *,
    connection: Connection | None = None,
) -> Iterator[Config]:
    """Yield Alembic configuration with migrations on the filesystem."""

    migration_resources = files(__package__).joinpath("alembic")
    with as_file(migration_resources) as migration_directory:
        config = Config()
        config.set_main_option("script_location", str(migration_directory))
        if connection is not None:
            config.attributes["connection"] = connection
        yield config


def upgrade_spool_database(engine: Engine) -> None:
    """Apply every committed spool migration to an engine."""

    # All build processes share this database. Serialize migration checks and
    # upgrades; SQLite transaction locks alone do not serialize schema inspection.
    path = Path(engine.url.database)
    with path.with_suffix(".migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with engine.begin() as connection:
            names = set(inspect(connection).get_table_names())
            revision = MigrationContext.configure(connection).get_current_revision()
            if names and revision is None:
                raise RuntimeError(
                    "Refusing an unversioned telemetry database; no changes made."
                )
            with alembic_config(connection=connection) as config:
                history = ScriptDirectory.from_config(config)
                if revision is not None:
                    history.get_revision(
                        revision
                    )  # Reject unknown revisions before writes.
                command.upgrade(config, _MIGRATION_TARGET)


def current_database_revision(path: Path | str) -> str | None:
    """Return the Alembic revision recorded by one spool database."""

    engine = create_spool_engine(path)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def migration_head_revision() -> str | None:
    """Return the single current head from the packaged migration history."""

    with alembic_config() as config:
        return ScriptDirectory.from_config(config).get_current_head()

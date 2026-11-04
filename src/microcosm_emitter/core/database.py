"""SQLAlchemy engine and session construction for the telemetry spool."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session, sessionmaker

from microcosm_emitter.core.constants import (
    DATABASE_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def sqlite_database_url(path: Path | str) -> URL:
    """Return a SQLAlchemy URL for a local SQLite spool path."""
    return URL.create("sqlite+pysqlite", database=str(Path(path)))


def create_spool_engine(path: Path | str) -> Engine:
    """Create the SQLAlchemy engine used by one emitter service process."""
    return create_engine(
        sqlite_database_url(path),
        connect_args={"timeout": DATABASE_TIMEOUT_SECONDS},
    )


def create_read_only_engine(path: Path) -> Engine:
    """Open an existing emitter database at ``path`` so that nothing can write it.

    In SQLite's read-only mode every write is refused and a missing file is not
    created, and no journal or other file is created beside it.
    """
    return create_engine(
        URL.create(
            "sqlite+pysqlite",
            database=path.resolve().as_uri(),
            query={"mode": "ro", "uri": "true"},
        ),
        connect_args={"timeout": DATABASE_TIMEOUT_SECONDS},
    )


def create_spool_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create short-lived ORM sessions bound to the spool engine."""
    return sessionmaker(engine, expire_on_commit=False)

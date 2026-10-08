"""SQLAlchemy engine and session construction for the telemetry spool."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from microcosm_provider_telemetry.service.constants import (
    DATABASE_TIMEOUT_SECONDS,
)


def sqlite_database_url(path: Path | str) -> URL:
    """Return a SQLAlchemy URL for a local SQLite spool path."""

    return URL.create("sqlite+pysqlite", database=str(Path(path)))


def create_spool_engine(path: Path | str) -> Engine:
    """Create the SQLAlchemy engine used by one emitter service process."""

    return create_engine(
        sqlite_database_url(path),
        connect_args={"timeout": DATABASE_TIMEOUT_SECONDS},
    )


def create_spool_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create short-lived ORM sessions bound to the spool engine."""

    return sessionmaker(engine, expire_on_commit=False)

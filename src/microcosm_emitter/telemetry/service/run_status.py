"""Read each run in the emitter's event queue file, changing nothing.

`microcosm-emitter status` prints what ``read_run_statuses`` returns, so that
Microcosm's Modal runner can record how many events a build left undelivered
without reading the file itself. The file is opened in SQLite's read-only
mode, never with ``EventSpool``: that would create a missing file, make the
file and its directory private to this user, migrate it, and prune events
past the retention limits. Reading it here sends, delivers, prunes, migrates
and creates nothing.

Only database and operating-system errors are turned into
``UnusableEventQueueFileError``; any other error is a defect and propagates.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Literal, TypedDict

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from microcosm_emitter.core.database import create_read_only_engine
from microcosm_emitter.core.migrations import known_revisions, recorded_revisions
from microcosm_emitter.core.models import TelemetryEventRecord, TelemetryRunRecord
from microcosm_emitter_interface.spool import default_spool_path

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection


class RunStatus(TypedDict):
    """One producer's run in the event queue file, as `status` prints it.

    ``reason`` is the run's ``local_only_reason``: why a ``local_only`` run is
    excluded from upload, or why a ``pending`` one waits, such as an expired
    collector token. ``undelivered_events`` counts the run's events still in
    the file, which the emitter deletes once the collector acknowledges them.
    """

    run_id: str
    producer_id: str
    upload_state: str
    reason: str | None
    undelivered_events: int


#: Why a file cannot be read as an event queue file: this user may not read
#: it; it is not one (not a regular file, not a SQLite database, or one with
#: no migration revision); it records a migration revision this emitter does
#: not know, as a newer release or another database would; a journal beside it
#: holds a write that did not finish, which only a connection that may write
#: can roll back; or another database or operating-system error.
Problem = Literal[
    "permission_denied",
    "not_an_event_queue_file",
    "unknown_revision",
    "unfinished_write",
    "unreadable",
]


class MissingEventQueueFileError(Exception):
    """No file exists at the path ``read_run_statuses`` was given."""


class UnusableEventQueueFileError(Exception):
    """The file cannot be read as an event queue file, for ``problem``.

    ``detail`` is the unknown revision for ``unknown_revision``, or the
    error's name for ``unreadable``; never the file's contents.
    """

    def __init__(self, problem: Problem, detail: str | None = None) -> None:
        """Record the problem and its detail."""
        super().__init__(problem)
        self.problem: Problem = problem
        self.detail = detail


def event_queue_file(path: Path | None) -> Path:
    """Return ``path``, or without one the file the emitter itself uses."""
    return default_spool_path() if path is None else path


def read_run_statuses(path: Path) -> list[RunStatus]:
    """Return every run in the event queue file at ``path``, by run and producer.

    Raises ``MissingEventQueueFileError`` when nothing exists at ``path``, and
    ``UnusableEventQueueFileError`` when the file cannot be read as an event
    queue file.
    """
    _require_readable_file(path)
    engine = create_read_only_engine(path)
    try:
        with engine.connect() as connection:
            _require_known_revision(connection)
            with Session(connection) as session:
                rows = session.execute(
                    select(
                        TelemetryRunRecord,
                        func.count(TelemetryEventRecord.event_id),
                    )
                    .outerjoin(TelemetryRunRecord.events)
                    .group_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
                    .order_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
                ).all()
                return [
                    {
                        "run_id": run.run_id,
                        "producer_id": run.producer_id,
                        "upload_state": run.upload_state,
                        "reason": run.local_only_reason,
                        "undelivered_events": undelivered_events,
                    }
                    for run, undelivered_events in rows
                ]
    except SQLAlchemyError as error:
        raise UnusableEventQueueFileError(*_database_problem(error)) from None
    except OSError as error:
        raise UnusableEventQueueFileError(*_os_problem(error)) from None
    finally:
        engine.dispose()


def _require_readable_file(path: Path) -> None:
    """Refuse a missing path, anything but a regular file, and an unreadable file.

    SQLite would report a file this user may not read only as one it cannot
    open.
    """
    try:
        details = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        raise MissingEventQueueFileError from None
    except OSError as error:
        raise UnusableEventQueueFileError(*_os_problem(error)) from None
    if not stat.S_ISREG(details.st_mode):
        raise UnusableEventQueueFileError(problem="not_an_event_queue_file")
    if not os.access(path, os.R_OK):
        raise UnusableEventQueueFileError(problem="permission_denied")


def _require_known_revision(connection: Connection) -> None:
    """Refuse a database without a migration revision, or with an unknown one.

    The event queue history of this emitter lacks the revision of a hand-over
    database, or of a file a newer release wrote.
    """
    revisions = recorded_revisions(connection)
    if not revisions:
        raise UnusableEventQueueFileError(problem="not_an_event_queue_file")
    unknown = sorted(set(revisions) - known_revisions())
    if unknown:
        raise UnusableEventQueueFileError(
            problem="unknown_revision", detail=", ".join(unknown)
        )


def _database_problem(error: SQLAlchemyError) -> tuple[Problem, str | None]:
    """Name a database error by SQLite's error code.

    The driver's exception carries the code's name; the emitter's modules do
    not import the driver.
    """
    name = getattr(getattr(error, "orig", None), "sqlite_errorname", None)
    if name == "SQLITE_READONLY_ROLLBACK":
        return "unfinished_write", None
    if name == "SQLITE_NOTADB":
        return "not_an_event_queue_file", None
    return "unreadable", name or type(error).__name__


def _os_problem(error: OSError) -> tuple[Problem, str | None]:
    if isinstance(error, PermissionError):
        return "permission_denied", None
    return "unreadable", type(error).__name__

"""Read each run in the emitter's event queue file, changing nothing.

`microcosm-emitter status` prints what ``read_run_statuses`` returns, so that
Microcosm's Modal runner can record how many events a build left undelivered
without reading the file itself. The file is opened in SQLite's read-only
mode, never with ``EventSpool``: that would create a missing file, make the
file and its directory private to this user, migrate it, and prune events
past the retention limits. Reading it here sends, delivers, prunes, migrates
and creates nothing, and reads only the five columns `status` prints.

Only database and operating-system errors are turned into
``UnusableEventQueueFileError``; any other error is a defect and propagates.
"""

from __future__ import annotations

import os
import stat
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

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
    collector token. ``undelivered_events`` counts the run's events the file
    still holds. The emitter deletes an event once the collector acknowledges
    it, and its pruning also deletes events that were never delivered: those
    older than the 7-day retention, and the oldest beyond the 100 MiB limit.
    So 0 means the file holds none of the run's events, not that every event
    was delivered.
    """

    run_id: str
    producer_id: str
    upload_state: str
    reason: str | None
    undelivered_events: int


class Problem(Enum):
    """Why a file cannot be read as an event queue file, by its message.

    Each member's value is the one line `status` prints on standard error,
    where ``{path}`` is the file and ``{detail}`` the unknown revision or the
    error's name, never the file's contents. So every problem has a message.
    """

    #: This user may not read the file, or search a directory above it.
    PERMISSION_DENIED = (
        "The event queue file at {path} cannot be read: permission denied."
    )
    #: Not a regular file, not a SQLite database, or a database without a
    #: migration revision.
    NOT_AN_EVENT_QUEUE_FILE = "The file at {path} is not an event queue file."
    #: A revision the event queue history of this emitter lacks, as a file a
    #: newer release wrote or a hand-over database records.
    UNKNOWN_REVISION = (
        "The event queue file at {path} records a migration revision ({detail}) "
        "that this emitter does not know: a newer emitter release wrote it, or "
        "it is another database."
    )
    #: A journal beside the file holds a write that did not finish, which only
    #: a connection that may write can roll back.
    UNFINISHED_WRITE = (
        "The event queue file at {path} cannot be read without changing it: the "
        "journal beside it holds a write that did not finish, which SQLite must "
        "roll back first, and `status` changes nothing."
    )
    #: Any other database or operating-system error, named by SQLite's error
    #: code or the error's type.
    UNREADABLE = "The event queue file at {path} cannot be read ({detail})."


class MissingEventQueueFileError(Exception):
    """No file exists at the path ``read_run_statuses`` was given."""


class UnusableEventQueueFileError(Exception):
    """The file cannot be read as an event queue file, for ``problem``.

    ``detail`` is the unknown revision for ``UNKNOWN_REVISION``, or the
    error's name for ``UNREADABLE``.
    """

    def __init__(self, problem: Problem, detail: str | None = None) -> None:
        """Record the problem and its detail."""
        super().__init__(problem.name)
        self.problem = problem
        self.detail = detail

    def message(self, path: Path) -> str:
        """Return the problem's line for standard error, naming ``path``."""
        return self.problem.value.format(path=path, detail=self.detail)


def event_queue_file(path: Path | None) -> Path:
    """Return ``path``, or without one the default path for this environment.

    That is ``default_spool_path()``, from this process's ``XDG_CACHE_HOME``
    and ``HOME``. A build computes its file the same way when it is given no
    path and hands it to the emitter it starts, so this is the build's file
    only when the build passed no path and ran with the same environment.
    """
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
            rows = connection.execute(
                select(
                    TelemetryRunRecord.run_id,
                    TelemetryRunRecord.producer_id,
                    TelemetryRunRecord.upload_state,
                    TelemetryRunRecord.local_only_reason,
                    func.count(TelemetryEventRecord.event_id),
                )
                .outerjoin(TelemetryRunRecord.events)
                .group_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
                .order_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
            ).all()
    except SQLAlchemyError as error:
        raise UnusableEventQueueFileError(*_database_problem(error)) from None
    except OSError as error:
        raise UnusableEventQueueFileError(*_os_problem(error)) from None
    finally:
        engine.dispose()
    return [
        {
            "run_id": run_id,
            "producer_id": producer_id,
            "upload_state": upload_state,
            "reason": reason,
            "undelivered_events": undelivered_events,
        }
        for run_id, producer_id, upload_state, reason, undelivered_events in rows
    ]


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
        raise UnusableEventQueueFileError(Problem.NOT_AN_EVENT_QUEUE_FILE)
    if not os.access(path, os.R_OK):
        raise UnusableEventQueueFileError(Problem.PERMISSION_DENIED)


def _require_known_revision(connection: Connection) -> None:
    """Refuse a database without a migration revision, or with an unknown one.

    The event queue history of this emitter lacks the revision of a hand-over
    database, or of a file a newer release wrote.
    """
    revisions = recorded_revisions(connection)
    if not revisions:
        raise UnusableEventQueueFileError(Problem.NOT_AN_EVENT_QUEUE_FILE)
    unknown = sorted(set(revisions) - known_revisions())
    if unknown:
        raise UnusableEventQueueFileError(
            Problem.UNKNOWN_REVISION, detail=", ".join(unknown)
        )


def _database_problem(error: SQLAlchemyError) -> tuple[Problem, str | None]:
    """Name a database error by SQLite's error code.

    The driver's exception carries the code's name; the emitter's modules do
    not import the driver.
    """
    name = getattr(getattr(error, "orig", None), "sqlite_errorname", None)
    if name == "SQLITE_READONLY_ROLLBACK":
        return Problem.UNFINISHED_WRITE, None
    if name == "SQLITE_NOTADB":
        return Problem.NOT_AN_EVENT_QUEUE_FILE, None
    return Problem.UNREADABLE, name or type(error).__name__


def _os_problem(error: OSError) -> tuple[Problem, str | None]:
    if isinstance(error, PermissionError):
        return Problem.PERMISSION_DENIED, None
    return Problem.UNREADABLE, type(error).__name__

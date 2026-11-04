"""`microcosm-emitter status`: each run in the event queue file, read-only.

Microcosm's Modal runner is to run it in the container once the build and
its emitter have finished, to record how many events each run left
undelivered, without reading the emitter's event queue file itself.
"""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Column, MetaData, String, Table, delete, insert, update
from sqlalchemy.orm import Session

from microcosm_emitter import cli
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.migrations import (
    migration_head_revision,
    upgrade_handover_database,
)
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.spool import EventSpool

NO_EVENT_QUEUE_FILE_EXIT_CODE = 8
UNUSABLE_EVENT_QUEUE_FILE_EXIT_CODE = 9
#: A revision no emitter release has, as an event queue file a newer release
#: wrote would record.
NEWER_REVISION = "20991231_99"

#: Each run the event queue file holds, as `status` prints it, in its order.
EXPECTED_RUNS = [
    {
        "run_id": "finished-run",
        "producer_id": "modal",
        "upload_state": "pending",
        "reason": None,
        "undelivered_events": 0,
    },
    {
        "run_id": "refused-run",
        "producer_id": "modal",
        "upload_state": "local_only",
        "reason": "run_registration_rejected",
        "undelivered_events": 1,
    },
    {
        "run_id": "waiting-run",
        "producer_id": "local",
        "upload_state": "pending",
        "reason": None,
        "undelivered_events": 1,
    },
    {
        "run_id": "waiting-run",
        "producer_id": "modal",
        "upload_state": "pending",
        "reason": "collector_token_expired",
        "undelivered_events": 2,
    },
]


def run_status(*arguments: str) -> int:
    """Run `microcosm-emitter status` and return its exit code.

    That includes the code of a usage error, which argparse exits with.
    """
    try:
        return cli.main(["status", *arguments])
    except SystemExit as exit_request:
        return exit_request.code


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test on any socket connection."""

    def refuse_connection(*arguments, **options):
        error_message = "status must not open a network connection"
        raise AssertionError(error_message)

    monkeypatch.setattr(socket.socket, "connect", refuse_connection)


def registration(run_id: str, producer_id: str) -> dict[str, object]:
    return TelemetryRun(
        run_id=run_id,
        country_code="US",
        pipeline="us_acs",
        producer_id=producer_id,
    ).as_registration()


def stage_event(stage_id: str) -> dict[str, object]:
    return {"event_type": "stage", "stage_id": stage_id, "status": "started"}


def write_event_queue_file(path: Path) -> None:
    """Queue the runs in ``EXPECTED_RUNS`` in an event queue file at ``path``.

    They are registered out of order, so the output's order comes from
    sorting.
    """
    queue = EventSpool(path)
    try:
        for run_id, producer_id, event_count in (
            ("waiting-run", "modal", 2),
            ("refused-run", "modal", 1),
            ("waiting-run", "local", 1),
            ("finished-run", "modal", 0),
        ):
            run = registration(run_id, producer_id)
            queue.register(run)
            for index in range(event_count):
                queue.append(run, stage_event(f"stage_{index}"))
        queue.record_pending_reason("waiting-run", "modal", "collector_token_expired")
        queue.make_local_only("refused-run", "modal", "run_registration_rejected")
    finally:
        queue.close()


def age_events(path: Path, days: int) -> None:
    """Make every queued event ``days`` old.

    Past the 7-day retention, opening the file for writing would prune them.
    """
    created_at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    engine = create_spool_engine(path)
    try:
        with Session(engine) as session, session.begin():
            session.execute(update(TelemetryEventRecord).values(created_at=created_at))
    finally:
        engine.dispose()


def record_registration_json(path: Path, text: str) -> None:
    """Store ``text`` as every run's registration JSON, as written."""
    engine = create_spool_engine(path)
    try:
        with engine.begin() as connection:
            runs = Table("telemetry_runs", MetaData(), autoload_with=connection)
            connection.execute(update(runs).values(registration_json=text))
    finally:
        engine.dispose()


def record_revision(path: Path, revision: str) -> None:
    """Record ``revision`` as the event queue file's migration revision."""
    engine = create_spool_engine(path)
    try:
        with engine.begin() as connection:
            version_table = Table(
                "alembic_version", MetaData(), autoload_with=connection
            )
            connection.execute(update(version_table).values(version_num=revision))
    finally:
        engine.dispose()


def leave_an_unfinished_write(path: Path) -> None:
    """Leave the journal of an unfinished write beside the event queue file.

    An emitter stopped during a commit leaves one.
    """
    journal = path.with_name(f"{path.name}-journal")
    engine = create_spool_engine(path)
    try:
        with engine.connect() as connection:
            # Without syncing, SQLite marks the journal as one to roll back
            # from its first write, as it does just before a commit changes
            # the database file.
            connection.exec_driver_sql("PRAGMA synchronous = OFF")
            connection.commit()
            connection.execute(delete(TelemetryEventRecord))
            unfinished = journal.read_bytes()
            connection.rollback()
    finally:
        engine.dispose()
    journal.write_bytes(unfinished)


def directory_state(directory: Path) -> list[tuple[str, int, int, str | None]]:
    """Return what reading a file in ``directory`` must leave unchanged.

    That is each entry's name, permission bits, size and, for a file this user
    may read, SHA-256.
    """
    state = []
    for entry in sorted(directory.iterdir()):
        details = entry.lstat()
        digest = (
            hashlib.sha256(entry.read_bytes()).hexdigest()
            if entry.is_file() and os.access(entry, os.R_OK)
            else None
        )
        state.append(
            (entry.name, stat.S_IMODE(details.st_mode), details.st_size, digest)
        )
    return state


def test_status_prints_each_run_and_changes_nothing(
    tmp_path, no_network, capsys
) -> None:
    path = tmp_path / "container" / "events.sqlite3"
    write_event_queue_file(path)
    # Older than the retention period: an emitter opening the file would
    # delete them before counting.
    age_events(path, days=8)
    # Modes the emitter's own opening would make private (0700 and 0600).
    path.parent.chmod(0o755)
    path.chmod(0o644)
    before = directory_state(path.parent)

    exit_code = run_status("--event-queue-file", str(path))

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert json.loads(output.out) == {"runs": EXPECTED_RUNS}
    assert output.out.count("\n") == 1
    assert output.err == ""
    assert directory_state(path.parent) == before


@pytest.mark.usefixtures("no_network")
@pytest.mark.parametrize(
    ("xdg_cache_home", "expected_root"),
    [("cache", "cache"), ("  ", "home/.cache"), (None, "home/.cache")],
    ids=["XDG_CACHE_HOME set", "XDG_CACHE_HOME blank", "XDG_CACHE_HOME unset"],
)
def test_status_reads_the_default_path_for_its_environment(
    tmp_path, monkeypatch, capsys, xdg_cache_home, expected_root
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if xdg_cache_home is None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    elif xdg_cache_home.strip():
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / xdg_cache_home))
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", xdg_cache_home)
    path = tmp_path / expected_root / "microcosm" / "telemetry" / "events.sqlite3"
    write_event_queue_file(path)

    exit_code = run_status()

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert json.loads(output.out) == {"runs": EXPECTED_RUNS}


@pytest.mark.parametrize("named", [True, False], ids=["named", "default"])
def test_status_exits_8_without_an_event_queue_file(
    tmp_path, no_network, monkeypatch, capsys, named
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if named:
        path = tmp_path / "absent" / "events.sqlite3"
        arguments = ["--event-queue-file", str(path)]
    else:
        path = tmp_path / "cache" / "microcosm" / "telemetry" / "events.sqlite3"
        arguments = []

    exit_code = run_status(*arguments)

    output = capsys.readouterr()
    assert exit_code == NO_EVENT_QUEUE_FILE_EXIT_CODE, output.err
    assert output.out == ""
    assert output.err.count("\n") == 1
    assert str(path) in output.err
    assert "no event queue file" in output.err.lower()
    # Nothing was created on the way.
    assert not (tmp_path / "absent").exists()
    assert not (tmp_path / "cache").exists()


def other_content(path: Path) -> None:
    path.write_bytes(b"not a database")


def zero_bytes(path: Path) -> None:
    path.write_bytes(b"")


def a_directory(path: Path) -> None:
    path.mkdir()


def a_handover_database(path: Path) -> None:
    engine = create_spool_engine(path)
    try:
        upgrade_handover_database(engine)
    finally:
        engine.dispose()


def a_newer_revision(path: Path) -> None:
    write_event_queue_file(path)
    record_revision(path, NEWER_REVISION)


def no_read_permission(path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root may read any file")
    write_event_queue_file(path)
    path.chmod(0o000)


def an_unfinished_write(path: Path) -> None:
    write_event_queue_file(path)
    leave_an_unfinished_write(path)


def a_known_revision_without_the_telemetry_tables(path: Path) -> None:
    engine = create_spool_engine(path)
    try:
        with engine.begin() as connection:
            version_table = Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(32), primary_key=True),
            )
            version_table.create(connection)
            connection.execute(
                insert(version_table).values(version_num=migration_head_revision())
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("prepare", "explanation"),
    [
        (other_content, "is not an event queue file"),
        (zero_bytes, "is not an event queue file"),
        (a_directory, "is not an event queue file"),
        (a_handover_database, "migration revision"),
        (a_newer_revision, f"migration revision ({NEWER_REVISION})"),
        (no_read_permission, "permission denied"),
        (an_unfinished_write, "a write that did not finish"),
        # Any other database error: the message names SQLite's error code.
        (
            a_known_revision_without_the_telemetry_tables,
            "cannot be read (SQLITE_ERROR)",
        ),
    ],
    ids=[
        "other content",
        "zero bytes",
        "a directory",
        "a hand-over database",
        "a newer revision",
        "no read permission",
        "an unfinished write",
        "a known revision without the telemetry tables",
    ],
)
def test_status_exits_9_for_a_file_it_cannot_use(
    tmp_path, no_network, capsys, prepare, explanation
) -> None:
    path = tmp_path / "container" / "events.sqlite3"
    path.parent.mkdir()
    prepare(path)
    before = directory_state(path.parent)

    exit_code = run_status("--event-queue-file", str(path))

    output = capsys.readouterr()
    assert exit_code == UNUSABLE_EVENT_QUEUE_FILE_EXIT_CODE, output.err
    assert output.out == ""
    assert output.err.count("\n") == 1
    assert str(path) in output.err
    assert explanation in output.err
    assert directory_state(path.parent) == before


def test_status_prints_a_run_whose_registration_is_not_an_object(tmp_path) -> None:
    """`status` reads only the columns it prints, never a run's registration.

    It runs as a program, as Microcosm's runner will run it, so an error that
    escaped the command would show as its exit code.
    """
    path = tmp_path / "container" / "events.sqlite3"
    write_event_queue_file(path)
    record_registration_json(path, "[]")

    answer = subprocess.run(
        [
            sys.executable,
            "-m",
            "microcosm_emitter.cli",
            "status",
            "--event-queue-file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert answer.returncode == 0, answer.stderr
    assert json.loads(answer.stdout) == {"runs": EXPECTED_RUNS}
    assert answer.stderr == ""


def test_status_help_names_the_event_queue_file(capsys) -> None:
    exit_code = run_status("--help")

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert "--event-queue-file" in output.out
    assert "event queue file" in output.out
    assert "spool" not in output.out.lower()

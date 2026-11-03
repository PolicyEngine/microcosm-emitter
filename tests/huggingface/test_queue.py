"""The durable hand-over queue, in its own database next to the spool database."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.migrations import current_database_revision
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.huggingface.service.queue import (
    MIN_COMMIT_SPACING_SECONDS,
    HandoverQueue,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPOSITORY = "policyengine/fake-staging"
OTHER_REPOSITORY = "policyengine/fake-private"


@pytest.fixture
def queue(tmp_path):
    handle = HandoverQueue(tmp_path / "events.handovers.sqlite3")
    yield handle
    handle.close()


def stage(queue, path_in_repo, content, *, now, **extra):
    """Stage one run file; ``extra`` may name only the options read below."""
    options = {
        "repository": extra.pop("repository", REPOSITORY),
        "run_id": extra.pop("run_id", "run-1"),
        "run_index_entry": extra.pop("run_index_entry", None),
        "flush": extra.pop("flush", True),
        "interval": extra.pop("interval", 30.0),
    }
    if extra:
        error_message = f"stage() got unexpected options: {sorted(extra)}"
        raise TypeError(error_message)
    queue.stage_run_files(files=[(path_in_repo, content)], now=now, **options)


def commit_everything(queue, *, repository=REPOSITORY, now, revision="commit-1"):
    queue.record_run_files_committed(
        repository,
        queue.pending_run_files(repository),
        queue.pending_run_index_entries(repository),
        revision=revision,
        now=now,
    )


def test_the_hand_over_database_leaves_the_spool_database_unchanged(tmp_path):
    path = tmp_path / "events.sqlite3"
    shutil.copyfile(FIXTURES / "microcosm-1099.sqlite3", path)
    spool_before = path.read_bytes()
    revision_before = current_database_revision(path)
    engine = create_spool_engine(path)
    with Session(engine) as session:
        events_before = session.scalar(
            select(func.count(TelemetryEventRecord.event_id))
        )
    engine.dispose()
    HandoverQueue(tmp_path / "events.handovers.sqlite3").close()
    assert path.read_bytes() == spool_before
    assert current_database_revision(path) == revision_before
    engine = create_spool_engine(path)
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count(TelemetryEventRecord.event_id)))
            == events_before
        )
    engine.dispose()


HAND_OVER_TABLES = {
    "staged_run_files",
    "staged_run_deliveries",
    "repository_commit_schedules",
    "handover_jobs",
    "handover_files",
}


def test_a_new_hand_over_database_starts_empty_and_migrates_on_first_open(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from microcosm_emitter.core.migrations import (
        HANDOVER_HISTORY,
        alembic_config,
        migration_head_revision,
    )

    path = tmp_path / "events.handovers.sqlite3"
    assert not path.exists()
    queue = HandoverQueue(path)
    try:
        assert current_database_revision(path) == migration_head_revision(
            HANDOVER_HISTORY
        )
        assert set(inspect(queue._engine).get_table_names()) == HAND_OVER_TABLES | {
            "alembic_version"
        }
        assert queue.repositories_with_pending_run_files() == []
        with (
            queue._engine.begin() as connection,
            alembic_config(connection=connection, history=HANDOVER_HISTORY) as config,
        ):
            command.check(config)
    finally:
        queue.close()
    reopened = HandoverQueue(path)
    reopened.close()
    assert current_database_revision(path) == migration_head_revision(HANDOVER_HISTORY)


def test_a_spool_database_is_refused_as_a_hand_over_database(tmp_path):
    from microcosm_emitter.telemetry.service.spool import EventSpool

    path = tmp_path / "events.sqlite3"
    EventSpool(path).close()
    revision = current_database_revision(path)
    with pytest.raises(Exception, match="revision"):
        HandoverQueue(path)
    assert current_database_revision(path) == revision


def test_latest_bytes_for_a_path_replace_earlier_pending_bytes(queue):
    stage(queue, "runs/run-1/progress.json", b'{"stage":"created"}', now=100)
    stage(queue, "runs/run-1/progress.json", b'{"stage":"calibrating"}', now=101)
    (pending,) = queue.pending_run_files(REPOSITORY)
    assert pending.path_in_repo == "runs/run-1/progress.json"
    assert pending.content == b'{"stage":"calibrating"}'
    assert pending.run_id == "run-1"


def test_bytes_equal_to_those_last_committed_are_not_pending(queue):
    stage(queue, "runs/run-1/run_manifest.json", b"{}", now=100)
    commit_everything(queue, now=100)
    stage(queue, "runs/run-1/run_manifest.json", b"{}", now=200)
    assert queue.pending_run_files(REPOSITORY) == []
    stage(queue, "runs/run-1/run_manifest.json", b'{"changed":true}', now=201)
    assert [entry.content for entry in queue.pending_run_files(REPOSITORY)] == [
        b'{"changed":true}'
    ]


def test_unforced_hand_overs_wait_for_the_interval_after_the_last_commit(queue):
    stage(queue, "runs/run-1/progress.json", b"1", now=1000, flush=False, interval=300)
    assert queue.due_repositories(1000) == [REPOSITORY]
    commit_everything(queue, now=1000)
    stage(queue, "runs/run-1/progress.json", b"2", now=1010, flush=False, interval=300)
    assert queue.due_repositories(1010) == []
    assert queue.due_repositories(1299) == []
    assert queue.due_repositories(1300) == [REPOSITORY]


def test_forced_hand_overs_keep_the_minimum_spacing_between_commits(queue):
    stage(queue, "runs/run-1/progress.json", b"1", now=1000)
    commit_everything(queue, now=1000)
    stage(queue, "runs/run-1/progress.json", b"2", now=1001)
    assert queue.due_repositories(1001) == []
    assert queue.due_repositories(1000 + MIN_COMMIT_SPACING_SECONDS) == [REPOSITORY]


def test_a_failed_commit_backs_off_before_the_repository_is_due_again(queue):
    stage(queue, "runs/run-1/progress.json", b"1", now=1000)
    queue.record_run_files_failed(
        REPOSITORY,
        queue.pending_run_files(REPOSITORY),
        queue.pending_run_index_entries(REPOSITORY),
        error_code="UPLOAD_FAILED",
        now=1000,
    )
    assert queue.due_repositories(1001) == []
    assert queue.due_repositories(1002) == [REPOSITORY]
    receipt = queue.run_receipt(REPOSITORY, "run-1")
    assert receipt["last_error_code"] == "UPLOAD_FAILED"
    assert receipt["commit_attempts"] == 1
    assert receipt["commits"] == 0
    assert receipt["pending_files"] == 1


def test_repositories_are_due_independently(queue):
    stage(queue, "runs/run-1/progress.json", b"1", now=1000)
    stage(queue, "runs/run-2/progress.json", b"1", now=1000, run_id="run-2")
    stage(
        queue,
        "runs/run-3/progress.json",
        b"1",
        now=1000,
        run_id="run-3",
        repository=OTHER_REPOSITORY,
    )
    assert queue.due_repositories(1000) == sorted([REPOSITORY, OTHER_REPOSITORY])
    assert {entry.run_id for entry in queue.pending_run_files(REPOSITORY)} == {
        "run-1",
        "run-2",
    }


def test_one_writer_holds_a_repository_at_a_time(tmp_path):
    first = HandoverQueue(tmp_path / "events.handovers.sqlite3")
    second = HandoverQueue(tmp_path / "events.handovers.sqlite3")
    try:
        lease = first.claim_repository(REPOSITORY, now=1000, lease_seconds=60)
        assert lease is not None
        assert second.claim_repository(REPOSITORY, now=1001, lease_seconds=60) is None
        assert second.claim_repository(OTHER_REPOSITORY, now=1001, lease_seconds=60)
        first.release_repository(REPOSITORY, lease)
        assert second.claim_repository(REPOSITORY, now=1002, lease_seconds=60)
        assert first.claim_repository(REPOSITORY, now=1003, lease_seconds=60) is None
        assert first.claim_repository(REPOSITORY, now=1063, lease_seconds=60)
    finally:
        first.close()
        second.close()


def test_run_index_entries_stay_pending_until_committed(queue):
    stage(
        queue,
        "runs/run-1/progress.json",
        b"1",
        now=1000,
        run_index_entry={"run_id": "run-1", "status": "running"},
    )
    assert queue.pending_run_index_entries(REPOSITORY) == [
        ("run-1", {"run_id": "run-1", "status": "running"})
    ]
    commit_everything(queue, now=1000)
    assert queue.pending_run_index_entries(REPOSITORY) == []


def test_a_newer_hand_over_during_a_commit_stays_pending(queue):
    stage(queue, "runs/run-1/progress.json", b"1", now=1000)
    claimed = queue.pending_run_files(REPOSITORY)
    stage(queue, "runs/run-1/progress.json", b"2", now=1001)
    queue.record_run_files_committed(
        REPOSITORY, claimed, [], revision="commit-1", now=1002
    )
    assert [entry.content for entry in queue.pending_run_files(REPOSITORY)] == [b"2"]
    assert queue.committed_run_files(REPOSITORY, "run-1") == {
        "runs/run-1/progress.json": claimed[0].sha256
    }


def test_earlier_bytes_handed_over_again_during_a_commit_are_committed_after_it(
    queue,
):
    """The repository holds X; Y is being committed when X is handed over again."""
    path_in_repo = "runs/run-1/progress.json"
    stage(queue, path_in_repo, b"X", now=1000)
    commit_everything(queue, now=1001, revision="commit-x")
    stage(queue, path_in_repo, b"Y", now=1100)
    claimed = queue.pending_run_files(REPOSITORY)
    stage(queue, path_in_repo, b"X", now=1101)
    queue.record_run_files_committed(
        REPOSITORY, claimed, [], revision="commit-y", now=1102
    )
    assert [entry.content for entry in queue.pending_run_files(REPOSITORY)] == [b"X"]


def test_retries_back_off_up_to_five_minutes():
    from microcosm_emitter.huggingface.service.queue import (
        MAX_RETRY_SECONDS,
        retry_delay,
    )

    assert MAX_RETRY_SECONDS == 300.0
    assert [retry_delay(failures) for failures in (1, 2, 8, 9, 64)] == [
        2.0,
        4.0,
        256.0,
        300.0,
        300.0,
    ]


def test_receipt_counts_committed_files_and_commits_per_run(queue):
    queue.stage_run_files(
        repository=REPOSITORY,
        run_id="run-1",
        files=[("runs/run-1/a.json", b"1"), ("runs/run-1/b.json", b"2")],
        run_index_entry=None,
        flush=True,
        interval=30,
        now=1000,
    )
    commit_everything(queue, now=1000, revision="commit-1")
    receipt = queue.run_receipt(REPOSITORY, "run-1")
    assert receipt == {
        "repository": REPOSITORY,
        "run_id": "run-1",
        "pending_files": 0,
        "files_attempted": 2,
        "files_committed": 2,
        "commit_attempts": 1,
        "commits": 1,
        "last_error_code": None,
        "last_commit": "commit-1",
    }


def test_jobs_are_idempotent_by_hand_over_and_survive_a_restart(tmp_path):
    path = tmp_path / "events.handovers.sqlite3"
    queue = HandoverQueue(path)
    request = {"repository": REPOSITORY, "run_id": "run-1"}
    first = queue.enqueue_job(
        handover_id="bundle-1",
        kind="dataset_bundle",
        target=REPOSITORY,
        request=request,
        receipt={"status": "pending"},
        now=1000,
    )
    again = queue.enqueue_job(
        handover_id="bundle-1",
        kind="dataset_bundle",
        target=REPOSITORY,
        request=request,
        receipt={"status": "pending"},
        now=1001,
    )
    assert first == again == {"status": "pending"}
    with pytest.raises(ValueError, match="conflicts"):
        queue.enqueue_job(
            handover_id="bundle-1",
            kind="dataset_bundle",
            target=REPOSITORY,
            request={"repository": REPOSITORY, "run_id": "other"},
            receipt={"status": "pending"},
            now=1002,
        )
    queue.close()
    restarted = HandoverQueue(path)
    try:
        job = restarted.claim_job(REPOSITORY, now=1003, lease_seconds=60)
        assert job is not None
        assert job.handover_id == "bundle-1"
        assert job.request == request
        assert job.kind == "dataset_bundle"
        assert restarted.claim_job(REPOSITORY, now=1004, lease_seconds=60) is None
        restarted.finish_job(
            job, receipt={"status": "pending", "error_code": "X"}, retry=True, now=1005
        )
        assert restarted.claim_job(REPOSITORY, now=1005, lease_seconds=60) is None
        retried = restarted.claim_job(REPOSITORY, now=1007, lease_seconds=60)
        assert retried is not None
        assert retried.attempts == 1
        restarted.finish_job(
            retried, receipt={"status": "uploaded"}, retry=False, now=1008
        )
        assert restarted.claim_job(REPOSITORY, now=5000, lease_seconds=60) is None
        assert restarted.job_receipt("bundle-1") == {"status": "uploaded"}
        assert restarted.due_repositories(5000) == []
    finally:
        restarted.close()


def test_an_expired_worker_cannot_finish_a_reclaimed_job(queue):
    queue.enqueue_job(
        handover_id="bundle-1",
        kind="dataset_bundle",
        target=REPOSITORY,
        request={},
        receipt={"status": "pending"},
        now=1000,
    )
    expired = queue.claim_job(REPOSITORY, now=1000, lease_seconds=5)
    current = queue.claim_job(REPOSITORY, now=1006, lease_seconds=60)
    assert current is not None
    queue.finish_job(expired, receipt={"status": "uploaded"}, retry=False, now=1007)
    assert queue.job_receipt("bundle-1") == {"status": "pending"}

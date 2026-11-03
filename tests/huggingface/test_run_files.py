"""Staging run files: Microcosm's paths and bytes, one commit per cycle."""

import json
import time
from datetime import UTC, datetime

import pytest
from fake_hugging_face import STAGING_REPOSITORY, FakeHubHTTPError

from microcosm_emitter.huggingface.service import requests as request_module
from microcosm_emitter_interface.staging import RunFile, StageRunFiles


def hand_over(controls, run_id, files, *, latest_pointer=None, **options):
    """Write ``files`` to a local run directory and hand them over.

    ``options`` may give the request's ``flush`` (true), ``interval`` (30
    seconds), ``run_index_entry`` (none) and ``prefix`` (``"runs"``), and
    nothing else: another name raises ``TypeError``, as an unknown keyword
    argument would.
    """
    prefix = options.pop("prefix", "runs")
    run_index_entry = options.pop("run_index_entry", None)
    flush = options.pop("flush", True)
    interval = options.pop("interval", 30.0)
    if options:
        error_message = f"hand_over() got unexpected options: {sorted(options)}"
        raise TypeError(error_message)
    directory = controls.tmp_path / "local" / run_id
    run_files = []
    for relative_path, content in files.items():
        local = directory / relative_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        run_files.append(RunFile(relative_path, local))
    pointer_path = None
    if latest_pointer is not None:
        pointer_path = directory / "latest_staging.json"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_bytes(latest_pointer)
    request = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id=run_id,
        files=tuple(run_files),
        prefix=prefix,
        latest_pointer=pointer_path,
        run_index_entry=run_index_entry,
        flush=flush,
        commit_interval_seconds=interval,
    )
    return controls.module.query(request.message())["receipt"]


def cycle(controls):
    controls.module.delivery.run_cycle()


def commits(controls):
    return controls.hub.commits(STAGING_REPOSITORY)


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat()


def test_hand_overs_within_one_cycle_reach_the_hub_as_one_commit(
    hugging_face_module,
):
    controls = hugging_face_module
    hand_over(controls, "run-1", {"progress.json": b'{"stage":"created"}'})
    hand_over(controls, "run-1", {"run_manifest.json": b'{"run_id":"run-1"}'})
    hand_over(
        controls,
        "run-1",
        {"progress.json": b'{"stage":"calibrating"}', "events.ndjson": b"{}\n"},
    )
    cycle(controls)
    (commit,) = commits(controls)
    assert commit.added == {
        "runs/run-1/progress.json": b'{"stage":"calibrating"}',
        "runs/run-1/run_manifest.json": b'{"run_id":"run-1"}',
        "runs/run-1/events.ndjson": b"{}\n",
    }
    assert commit.message == "Stage run files for run-1"


def test_staging_v2_bundle_keeps_its_paths_and_bytes(hugging_face_module):
    controls = hugging_face_module
    files = {
        "run_manifest.json": (
            b'{\n  "schema_name": "microcosm.staging.run-manifest"\n}\n'
        ),
        "progress.json": b'{\n  "schema_name": "microcosm.staging.progress"\n}\n',
        "events.ndjson": b'{"sequence":1}\n{"sequence":2}\n',
        "calibration_progress.json": b'{\n  "events": []\n}\n',
        "artifacts/fit_summary.json": b'{\n  "final_loss": 0.5\n}\n',
    }
    receipt = hand_over(controls, "uk-frs-calibration-attempt-fixture", files)
    assert receipt["status"] == "recorded"
    assert receipt["pending_files"] == len(files)
    cycle(controls)
    (commit,) = commits(controls)
    assert commit.added == {
        f"runs/uk-frs-calibration-attempt-fixture/{name}": content
        for name, content in files.items()
    }
    receipt = controls.module.query(
        {
            "protocol_version": 1,
            "command": "run_files_receipt",
            "repository": STAGING_REPOSITORY,
            "run_id": "uk-frs-calibration-attempt-fixture",
        }
    )["receipt"]
    assert receipt["pending_files"] == 0
    assert receipt["files_committed"] == len(files)
    assert receipt["commits"] == 1
    assert receipt["last_commit"] == commit.oid


def test_unchanged_files_are_not_committed_again(hugging_face_module):
    controls = hugging_face_module
    hand_over(controls, "run-1", {"run_manifest.json": b"{}", "progress.json": b"1"})
    cycle(controls)
    controls.clock.advance(60)
    hand_over(controls, "run-1", {"run_manifest.json": b"{}", "progress.json": b"2"})
    cycle(controls)
    assert [commit.added for commit in commits(controls)][-1] == {
        "runs/run-1/progress.json": b"2"
    }


def test_unforced_hand_overs_wait_for_the_interval_and_are_coalesced(
    hugging_face_module,
):
    controls = hugging_face_module
    hand_over(controls, "run-1", {"progress.json": b"0"})
    cycle(controls)
    for value in (b"1", b"2", b"3"):
        controls.clock.advance(20)
        hand_over(
            controls, "run-1", {"progress.json": value}, flush=False, interval=300
        )
        cycle(controls)
    assert len(commits(controls)) == 1
    controls.clock.advance(300)
    cycle(controls)
    assert [commit.added for commit in commits(controls)] == [
        {"runs/run-1/progress.json": b"0"},
        {"runs/run-1/progress.json": b"3"},
    ]


def test_staging_v1_pointer_and_runs_index_join_the_run_files_commit(
    hugging_face_module,
):
    controls = hugging_face_module
    other_run = {
        "run_id": "run-older",
        "status": "passed",
        "updated_at": "2026-10-01T00:00:00+00:00",
    }
    controls.hub.write_directly(
        STAGING_REPOSITORY,
        {
            "runs.json": json.dumps(
                {"schema_version": 1, "updated_at": "x", "runs": [other_run]}, indent=1
            ).encode()
        },
    )
    this_run = {
        "run_id": "us-run",
        "candidate_release_id": "populace-us-fixture",
        "status": "running",
        "stage": "created",
        "started_at": "2026-10-20T00:00:00+00:00",
        "updated_at": "2026-10-20T00:00:00+00:00",
        "progress_path": "runs/us-run/progress.json",
        "run_manifest_path": "runs/us-run/run_manifest.json",
    }
    pointer = json.dumps({"schema_version": 1, "run_id": "us-run"}, indent=1).encode()
    hand_over(
        controls,
        "us-run",
        {"progress.json": b"{}"},
        latest_pointer=pointer,
        run_index_entry=this_run,
    )
    cycle(controls)
    commit = commits(controls)[-1]
    assert commit.added["runs/us-run/progress.json"] == b"{}"
    assert commit.added["latest_staging.json"] == pointer
    expected_index = json.dumps(
        {
            "schema_version": 1,
            "updated_at": iso(controls.clock.now),
            "runs": [this_run, other_run],
        },
        indent=1,
        allow_nan=False,
    ).encode()
    assert commit.added["runs.json"] == expected_index
    (create_commit,) = [
        arguments
        for method, arguments in controls.hub.calls
        if method == "create_commit"
    ]
    assert create_commit["parent_commit"] is not None


def test_staging_v1_runs_index_starts_empty_without_a_repository_copy(
    hugging_face_module,
):
    controls = hugging_face_module
    entry = {"run_id": "us-run", "updated_at": "2026-10-20T00:00:00+00:00"}
    hand_over(controls, "us-run", {}, run_index_entry=entry)
    cycle(controls)
    (commit,) = commits(controls)
    assert json.loads(commit.added["runs.json"])["runs"] == [entry]


@pytest.mark.parametrize(
    "transient_error",
    ["connection error", "rate limited", "service unavailable"],
)
def test_a_transient_download_error_never_starts_the_runs_index_empty(
    hugging_face_module, transient_error
):
    """huggingface_hub reports each of these as ``LocalEntryNotFoundError``."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    controls = hugging_face_module
    other_run = {"run_id": "run-older", "updated_at": "2026-10-01T00:00:00+00:00"}
    controls.hub.write_directly(
        STAGING_REPOSITORY,
        {
            "runs.json": json.dumps(
                {"schema_version": 1, "updated_at": "x", "runs": [other_run]}, indent=1
            ).encode()
        },
    )
    before = len(commits(controls))
    controls.hub.fail_next("hf_hub_download", LocalEntryNotFoundError(transient_error))
    entry = {"run_id": "us-run", "updated_at": "2026-10-20T00:00:00+00:00"}
    hand_over(controls, "us-run", {"progress.json": b"{}"}, run_index_entry=entry)
    cycle(controls)
    assert len(commits(controls)) == before
    receipt = controls.module.queue.run_receipt(STAGING_REPOSITORY, "us-run")
    assert receipt["last_error_code"] == "UPLOAD_FAILED"
    assert receipt["pending_files"] == 2
    controls.clock.advance(2)
    cycle(controls)
    index = json.loads(controls.hub.files(STAGING_REPOSITORY)["runs.json"])
    assert index["runs"] == [entry, other_run]


def test_runs_index_written_by_another_build_meanwhile_is_kept(hugging_face_module):
    controls = hugging_face_module
    concurrent_run = {"run_id": "concurrent", "updated_at": "2026-10-19T00:00:00Z"}
    controls.hub.run_before_next_commit(
        lambda: controls.hub.write_directly(
            STAGING_REPOSITORY,
            {
                "runs.json": json.dumps(
                    {"schema_version": 1, "runs": [concurrent_run]}
                ).encode()
            },
            message="Another build's index",
        )
    )
    entry = {"run_id": "us-run", "updated_at": "2026-10-20T00:00:00+00:00"}
    hand_over(controls, "us-run", {"progress.json": b"{}"}, run_index_entry=entry)
    cycle(controls)
    ours = [
        commit
        for commit in commits(controls)
        if commit.message == "Stage run files for us-run"
    ]
    assert len(ours) == 1
    assert json.loads(ours[0].added["runs.json"])["runs"] == [entry, concurrent_run]


def test_a_failed_commit_keeps_the_files_queued_and_retries_after_backoff(
    hugging_face_module,
):
    controls = hugging_face_module
    controls.hub.fail_next("create_commit", FakeHubHTTPError(503))
    hand_over(controls, "run-1", {"progress.json": b"1"})
    cycle(controls)
    assert commits(controls) == []
    receipt = controls.module.queue.run_receipt(STAGING_REPOSITORY, "run-1")
    assert receipt["last_error_code"] == "UPLOAD_FAILED"
    assert receipt["pending_files"] == 1
    cycle(controls)
    assert commits(controls) == []
    controls.clock.advance(2)
    cycle(controls)
    assert len(commits(controls)) == 1
    receipt = controls.module.queue.run_receipt(STAGING_REPOSITORY, "run-1")
    assert receipt["last_error_code"] is None
    assert receipt["commit_attempts"] == 2
    assert receipt["commits"] == 1


def test_worker_errors_never_stop_the_module(hugging_face_module, capsys):
    controls = hugging_face_module
    controls.hub.fail_next("create_commit", RuntimeError("synthetic"))
    hand_over(controls, "run-1", {"progress.json": b"1"})
    controls.module.tick(time.monotonic())
    controls.module.tick(time.monotonic())
    assert commits(controls) == []
    assert "synthetic" not in capsys.readouterr().err


def test_without_a_credential_files_stay_queued_and_one_warning_is_printed(
    hugging_face_module, capsys
):
    controls = hugging_face_module
    controls.remove_credential()
    hand_over(controls, "run-1", {"progress.json": b"1"})
    cycle(controls)
    controls.clock.advance(5)
    cycle(controls)
    assert commits(controls) == []
    receipt = controls.module.queue.run_receipt(STAGING_REPOSITORY, "run-1")
    assert receipt["last_error_code"] == "CREDENTIAL_MISSING"
    assert capsys.readouterr().err.count("no Hugging Face credential") == 1
    controls.restore_credential()
    controls.clock.advance(300)
    cycle(controls)
    assert len(commits(controls)) == 1


def test_close_commits_what_is_pending_without_waiting_for_the_interval(
    hugging_face_module,
):
    controls = hugging_face_module
    hand_over(controls, "run-1", {"progress.json": b"1"})
    cycle(controls)
    controls.clock.advance(1)
    hand_over(controls, "run-1", {"progress.json": b"2"}, flush=False, interval=300)
    controls.module.close(time.monotonic() + 5)
    assert [commit.added for commit in commits(controls)][-1] == {
        "runs/run-1/progress.json": b"2"
    }


def test_read_back_compares_the_hub_with_the_committed_bytes(hugging_face_module):
    controls = hugging_face_module
    query = {
        "protocol_version": 1,
        "command": "read_back_run_files",
        "repository": STAGING_REPOSITORY,
        "run_id": "run-1",
    }
    hand_over(controls, "run-1", {"progress.json": b"1", "run_manifest.json": b"{}"})
    assert controls.module.query(query)["read_back"]["status"] == "pending"
    cycle(controls)
    assert controls.module.query(query)["read_back"] == {
        "status": "passed",
        "files": 2,
        "mismatched_paths": [],
        "error_code": None,
    }
    controls.hub.write_directly(
        STAGING_REPOSITORY, {"runs/run-1/progress.json": b"changed"}
    )
    assert controls.module.query(query)["read_back"] == {
        "status": "failed",
        "files": 2,
        "mismatched_paths": ["runs/run-1/progress.json"],
        "error_code": None,
    }


def read_back(controls, run_id):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "read_back_run_files",
            "repository": STAGING_REPOSITORY,
            "run_id": run_id,
        }
    )["read_back"]


NOTHING_COMMITTED = {
    "status": "failed",
    "files": 0,
    "mismatched_paths": [],
    "error_code": "NO_COMMITTED_FILES",
}


def test_read_back_of_a_run_this_emitter_never_committed_does_not_pass(
    hugging_face_module,
):
    """For example a run handed over to another machine's emitter."""
    controls = hugging_face_module
    assert read_back(controls, "unknown-run") == NOTHING_COMMITTED


def test_read_back_after_a_refused_hand_over_does_not_pass(hugging_face_module):
    controls = hugging_face_module
    refused = controls.module.query(
        {
            "protocol_version": 1,
            "command": "stage_run_files",
            "repository": STAGING_REPOSITORY,
            "run_id": "run-1",
            "prefix": "runs",
            "files": [
                {"relative_path": "progress.json", "local_path": "/nonexistent/p"}
            ],
            "latest_pointer": None,
            "run_index_entry": None,
            "flush": True,
            "commit_interval_seconds": 30.0,
        }
    )["receipt"]
    assert refused["status"] == "refused"
    cycle(controls)
    assert read_back(controls, "run-1") == NOTHING_COMMITTED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_path", "../escape.json"),
        ("relative_path", "/absolute.json"),
        ("relative_path", "nested/../../escape.json"),
        ("relative_path", "back\\slash.json"),
        ("relative_path", ""),
        ("prefix", "releases"),
        ("prefix", "../runs"),
        ("prefix", ""),
        ("run_id", "../run"),
        ("repository", "no-owner"),
        ("commit_interval_seconds", -1),
        ("flush", "yes"),
    ],
)
def test_requests_that_could_leave_the_run_folder_are_refused(
    hugging_face_module, field, value
):
    controls = hugging_face_module
    local = controls.tmp_path / "progress.json"
    local.write_bytes(b"{}")
    message = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id="run-1",
        files=(RunFile("progress.json", local),),
    ).message()
    if field == "relative_path":
        message["files"][0]["relative_path"] = value
    else:
        message[field] = value
    receipt = controls.module.query(message)["receipt"]
    assert receipt["status"] == "refused"
    assert receipt["refusal_code"] == "INVALID_REQUEST"
    assert controls.module.queue.pending_run_files(STAGING_REPOSITORY) == []


def test_a_missing_local_file_refuses_the_whole_hand_over(hugging_face_module):
    controls = hugging_face_module
    present = controls.tmp_path / "present.json"
    present.write_bytes(b"{}")
    message = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id="run-1",
        files=(
            RunFile("present.json", present),
            RunFile("missing.json", controls.tmp_path / "missing.json"),
        ),
    ).message()
    receipt = controls.module.query(message)["receipt"]
    assert (receipt["status"], receipt["refusal_code"]) == (
        "refused",
        "LOCAL_FILE_MISSING",
    )
    assert controls.module.queue.pending_run_files(STAGING_REPOSITORY) == []


def test_files_above_the_transport_limit_are_refused(hugging_face_module, monkeypatch):
    controls = hugging_face_module
    monkeypatch.setattr(request_module, "MAX_RUN_FILE_BYTES", 4)
    receipt = hand_over(controls, "run-1", {"progress.json": b"12345"})
    assert (receipt["status"], receipt["refusal_code"]) == ("refused", "FILE_TOO_LARGE")


def test_unsupported_protocol_versions_are_refused(hugging_face_module):
    controls = hugging_face_module
    local = controls.tmp_path / "progress.json"
    local.write_bytes(b"{}")
    message = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id="run-1",
        files=(RunFile("progress.json", local),),
    ).message()
    message["protocol_version"] = 99
    receipt = controls.module.query(message)["receipt"]
    assert receipt["refusal_code"] == "UNSUPPORTED_PROTOCOL_VERSION"

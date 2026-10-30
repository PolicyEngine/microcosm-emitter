"""The service owns lifecycle interpretation, sanitization, and atomic persistence."""

import json
import os
import time
from datetime import timedelta
from unittest.mock import Mock

import pytest
from given_token import given_token_variables, set_variables
from sqlalchemy import event as sqlalchemy_event

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.module import TelemetryModule


@pytest.fixture
def module(tmp_path):
    run = TelemetryRun("test-run", "UK", "test", producer_id="test-producer")
    module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "heartbeat_seconds": 1,
            "development_collector_url": "http://127.0.0.1:1",
            "identity": {"host": {"cpu_count": 8}, "HF_TOKEN": "hf_identity_secret"},
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    module.delivery = Mock()
    yield module
    module.close(time.monotonic())


def events(module):
    return module.spool.batch("test-run", "test-producer")


def test_service_records_start_and_sanitizes_identity(module):
    event = events(module)[0]
    assert (event["event_type"], event["stage_id"], event["status"]) == (
        "run",
        "created",
        "started",
    )
    assert event["details"]["identity"]["host"]["cpu_count"] == 8
    assert event["details"]["identity"]["HF_TOKEN"] == "[redacted]"


def test_service_owns_stage_transitions_and_completion(module):
    for stage in ("load", "compile", "compile"):
        module.handle_message(
            {"command": "transition_stage", "stage_id": stage, "status": "running"}
        )
    module.handle_message({"command": "complete"})
    assert [(event["stage_id"], event["status"]) for event in events(module)] == [
        ("created", "started"),
        ("load", "started"),
        ("load", "completed"),
        ("compile", "started"),
        ("compile", "progress"),
        ("compile", "completed"),
        ("complete", "completed"),
    ]
    module.parent_exited()
    module.tick(time.monotonic() + 120)
    assert len(events(module)) == 7


def test_failure_closes_active_stage_and_redacts_before_storage(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.handle_message(
        {
            "command": "fail",
            "message": "Bearer hf_abcdefghijk",
            "error_type": "ValueError",
        }
    )
    assert [(event["event_type"], event["status"]) for event in events(module)[1:]] == [
        ("stage", "started"),
        ("stage", "failed"),
        ("run", "failed"),
    ]
    assert events(module)[-1]["details"]["failed_during"] == "compile"
    assert events(module)[-1]["details"]["error_type"] == "ValueError"
    assert "hf_abcdefghijk" not in json.dumps(events(module))


def test_calibration_requests_are_interpreted_by_service(module):
    module.handle_message(
        {"command": "transition_calibration_progress", "event": {"kind": "other"}}
    )
    assert len(events(module)) == 1
    for epoch in (1, 2):
        module.handle_message(
            {
                "command": "transition_calibration_progress",
                "event": {"kind": "calibration_epoch", "epoch": epoch},
            }
        )
    module.handle_message(
        {
            "command": "progress",
            "stage_id": "compile",
            "done": 2,
            "total": 4,
            "unit": "batches",
        }
    )
    assert [event["event_type"] for event in events(module)] == [
        "run",
        "stage",
        "calibration",
        "calibration",
        "progress",
    ]
    assert events(module)[-1]["details"]["done"] == 2


def test_event_and_stage_requests_are_sanitized_in_service(module):
    module.handle_message(
        {
            "command": "stage",
            "stage_id": "compile",
            "details": {
                "HF_TOKEN": "hf_private_token",
                "traceback": "private stack",
                **{f"large_{index}": "x" * 2000 for index in range(10)},
            },
        }
    )
    details = events(module)[-1]["details"]
    assert details["HF_TOKEN"] == "[redacted]"
    assert details["traceback"] == "[redacted]"
    assert details["telemetry_details_truncated"]
    assert len(json.dumps(details).encode()) <= 8192
    module.handle_message(
        {
            "command": "emit",
            "event_type": "progress",
            "status": "progress",
            "stage_id": "compile",
            "message": "credential=private",
        }
    )
    assert events(module)[-1]["message"] == "[redacted]"


def test_parent_exit_records_active_stage_once(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.parent_exited()
    module.parent_exited()
    assert len(events(module)) == 4
    assert events(module)[-2]["status"] == "failed"
    assert events(module)[-1]["details"]["failed_during"] == "compile"
    assert events(module)[-1]["details"]["failure_class"] == "unexpected_process_exit"


def test_heartbeat_has_resources_and_current_stage(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.tick(time.monotonic() + 120)
    event = events(module)[-1]
    assert event["event_type"] == "heartbeat"
    assert event["stage_id"] == "compile"
    assert event["resources"]["rss_bytes"] > 0


def test_persistence_failure_rolls_back_events_sequences_and_lifecycle(
    module, monkeypatch
):
    module.handle_message({"command": "transition_stage", "stage_id": "load"})
    before = events(module)
    original = module.spool.append_many

    def fail_second(registration, batch, **kwargs):
        batch = [dict(event) for event in batch]
        del batch[1]["status"]
        return original(registration, batch, **kwargs)

    monkeypatch.setattr(module.spool, "append_many", fail_second)
    with pytest.raises(KeyError):
        module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    assert events(module) == before
    assert module.lifecycle.transition_stage == "load"
    monkeypatch.setattr(module.spool, "append_many", original)
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    assert [event["sequence"] for event in events(module)] == [1, 2, 3, 4]
    assert [(event["stage_id"], event["status"]) for event in events(module)[-2:]] == [
        ("load", "completed"),
        ("compile", "started"),
    ]


def test_close_does_not_wait_for_a_given_token_that_expired(tmp_path, monkeypatch):
    # Its events cannot be delivered from here, so the build's exit is not
    # held for the drain period.
    set_variables(
        monkeypatch,
        given_token_variables("http://127.0.0.1:1", expires_in=timedelta(minutes=-1)),
    )
    run = TelemetryRun("given-token-run", "US", "test", producer_id="producer")
    given_token_module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "development_collector_url": "http://127.0.0.1:1",
        },
        ModuleContext(os.getpid()),
    )
    given_token_module.initialize()
    given_token_module.tick(time.monotonic())

    started = time.monotonic()
    given_token_module.close(started + 5)

    assert time.monotonic() - started < 1


def test_module_close_disposes_database_when_delivery_fails(module):
    module.delivery.flush_once.side_effect = OSError("network")
    module.spool.close = Mock(wraps=module.spool.close)
    with pytest.raises(OSError, match=r"^network$"):
        module.close(time.monotonic() + 1)
    module.spool.close.assert_called_once()


def test_failed_commit_preserves_active_stage_and_allows_completion_retry(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    before = events(module)

    def fail_commit(session):
        session.flush()
        error_message = "simulated commit failure"
        raise OSError(error_message)

    factory = module.spool._session_factory
    sqlalchemy_event.listen(factory, "before_commit", fail_commit)
    try:
        with pytest.raises(OSError, match="simulated commit failure"):
            module.handle_message({"command": "complete"})
    finally:
        sqlalchemy_event.remove(factory, "before_commit", fail_commit)

    assert events(module) == before
    assert not module.lifecycle.finished
    assert module.lifecycle.transition_stage == "compile"
    module.handle_message({"command": "complete"})
    assert [event["sequence"] for event in events(module)] == [1, 2, 3, 4]
    assert module.lifecycle.finished


def test_explicit_progress_keeps_stage_active_until_completion(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.handle_message(
        {"command": "transition_stage", "stage_id": "compile", "status": "progress"}
    )
    assert module.lifecycle.transition_stage == "compile"
    module.handle_message({"command": "complete"})
    assert [(event["stage_id"], event["status"]) for event in events(module)[-2:]] == [
        ("compile", "completed"),
        ("complete", "completed"),
    ]


@pytest.mark.parametrize(
    "stage_id",
    [
        "hf_download",
        "hf_private_stage",
        "secret:compile",
        "Compile.targets-1",
        "x" * 192,
    ],
)
def test_valid_stage_identifiers_are_preserved_in_events_and_heartbeats(
    module, stage_id
):
    module.handle_message(
        {
            "command": "transition_stage",
            "stage_id": stage_id,
            "message": "Bearer hf_private_credentials",
            "details": {"HF_TOKEN": "hf_detail_credentials"},
        }
    )
    module.handle_message(
        {"command": "progress", "stage_id": stage_id, "done": 1, "total": 2}
    )
    module.handle_message(
        {
            "command": "emit",
            "event_type": "stage",
            "status": "progress",
            "stage_id": stage_id,
        }
    )
    module.tick(time.monotonic() + 120)
    module.handle_message({"command": "complete"})

    recorded = events(module)
    assert [(event["event_type"], event["stage_id"]) for event in recorded[1:-1]] == [
        ("stage", stage_id),
        ("progress", stage_id),
        ("stage", stage_id),
        ("heartbeat", stage_id),
        ("stage", stage_id),
    ]
    assert recorded[1]["message"] == "[redacted]"
    assert recorded[1]["details"]["HF_TOKEN"] == "[redacted]"
    assert "hf_private_credentials" not in json.dumps(recorded)
    assert "hf_detail_credentials" not in json.dumps(recorded)
    assert recorded[-1]["status"] == "completed"


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"event_type": "run", "status": "started"},
        {"command": "unknown"},
        {"command": "emit", "event_type": "bad", "status": "started"},
        {"command": "stage", "stage_id": "compile", "status": "bad"},
        {"command": "stage", "stage_id": "compile", "details": []},
    ],
)
def test_invalid_request_never_enters_queue(module, message):
    before = events(module)
    with pytest.raises((ValueError, TypeError)):
        module.handle_message(message)
    assert events(module) == before


@pytest.mark.parametrize(
    "stage_id",
    ["", "compile targets", "compile\n", "_compile", "compile/targets", "x" * 193],
)
@pytest.mark.parametrize("command", ["stage", "transition_stage", "progress", "emit"])
def test_invalid_stage_identifier_preserves_queue_and_lifecycle(
    module, stage_id, command
):
    module.handle_message({"command": "transition_stage", "stage_id": "load"})
    before = events(module)
    state = module.lifecycle
    request = {
        "command": command,
        "event_type": "stage",
        "status": "started",
        "stage_id": stage_id,
        "done": 1,
        "total": 2,
    }

    with pytest.raises(ValueError, match="stage_id"):
        module.handle_message(request)

    assert events(module) == before
    assert module.lifecycle == state
    module.handle_message({"command": "complete"})
    assert [event["sequence"] for event in events(module)] == [1, 2, 3, 4]
    assert events(module)[-1]["status"] == "completed"


@pytest.mark.parametrize("active", [False, True])
def test_block_closes_active_stage_and_finishes_without_failure(module, active):
    if active:
        module.handle_message(
            {"command": "transition_stage", "stage_id": "hf_download"}
        )
    module.handle_message(
        {
            "command": "block",
            "phase": "preflight",
            "blocking_gate_ids": ["uk_local_target_fit"],
            "gate_statuses": {"uk_local_target_fit": "failed"},
        }
    )
    recorded = events(module)
    assert (
        recorded[-1]["event_type"],
        recorded[-1]["stage_id"],
        recorded[-1]["status"],
    ) == ("run", "blocked", "blocked")
    assert recorded[-1]["details"] == {
        "phase": "preflight",
        "blocking_gate_ids": ["uk_local_target_fit"],
        "blocking_failure_count": 1,
        "gate_statuses": {"uk_local_target_fit": "failed"},
    }
    if active:
        assert (recorded[-2]["stage_id"], recorded[-2]["status"]) == (
            "hf_download",
            "completed",
        )
    assert module.lifecycle.finished
    assert module.lifecycle.transition_stage is None
    module.parent_exited()
    module.tick(time.monotonic() + 120)
    module.handle_message({"command": "complete"})
    assert events(module) == recorded


def test_block_accepts_empty_ids_with_explicit_positive_count(module):
    module.handle_message(
        {
            "command": "block",
            "phase": "post_solve",
            "blocking_gate_ids": [],
            "blocking_failure_count": 3,
        }
    )
    assert events(module)[-1]["details"] == {
        "phase": "post_solve",
        "blocking_gate_ids": [],
        "blocking_failure_count": 3,
    }


@pytest.mark.parametrize(
    ("failure_class", "error_code"),
    [
        ("error", "BUILD_FAILED"),
        ("interrupted", "BUILD_INTERRUPTED"),
        ("terminated", "BUILD_TERMINATED"),
        ("oom", "BUILD_OUT_OF_MEMORY"),
    ],
)
def test_failure_code_persists_on_stage_and_run(module, failure_class, error_code):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.handle_message(
        {
            "command": "fail",
            "error_type": "MemoryError",
            "message": "Bearer hf_private_credentials",
            "failure_class": failure_class,
            "error_code": error_code,
        }
    )
    assert [event["details"]["error_code"] for event in events(module)[-2:]] == [
        error_code,
        error_code,
    ]
    assert events(module)[-1]["details"]["failure_class"] == failure_class
    assert events(module)[-1]["message"] == "[redacted]"


def test_block_commit_failure_preserves_stage_and_sequence(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    recorded = events(module)

    def fail_commit(session):
        session.flush()
        error_message = "simulated block commit failure"
        raise OSError(error_message)

    factory = module.spool._session_factory
    sqlalchemy_event.listen(factory, "before_commit", fail_commit)
    try:
        with pytest.raises(OSError, match="block commit"):
            module.handle_message(
                {
                    "command": "block",
                    "phase": "preflight",
                    "blocking_gate_ids": ["check"],
                }
            )
    finally:
        sqlalchemy_event.remove(factory, "before_commit", fail_commit)
    assert events(module) == recorded
    assert not module.lifecycle.finished
    assert module.lifecycle.transition_stage == "compile"
    module.handle_message(
        {"command": "block", "phase": "preflight", "blocking_gate_ids": ["check"]}
    )
    assert [event["sequence"] for event in events(module)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "details",
    [
        {"phase": "bad phase"},
        {"phase": "hf_private_credentials"},
        {"blocking_failure_count": 0},
        {"blocking_failure_count": True},
        {"blocking_failure_count": "1"},
        {"blocking_gate_ids": [""]},
        {"blocking_gate_ids": ["x" * 201]},
        {"blocking_gate_ids": [1]},
        {"blocking_gate_ids": "check"},
        {"blocking_gate_ids": None},
        {"blocking_gate_ids": [], "blocking_failure_count": None},
        {"gate_statuses": {f"check-{number}": "x" * 2000 for number in range(10)}},
    ],
)
def test_invalid_block_cannot_poison_queue_or_lifecycle(module, details):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    recorded, state = events(module), module.lifecycle
    request = {
        "command": "block",
        "phase": "preflight",
        "blocking_gate_ids": ["check"],
        **details,
    }
    with pytest.raises((ValueError, TypeError)):
        module.handle_message(request)
    assert events(module) == recorded
    assert module.lifecycle == state


def test_uncoded_failure_omits_optional_error_code(module):
    module.handle_message({"command": "fail", "message": "failed"})
    assert "error_code" not in events(module)[-1]["details"]

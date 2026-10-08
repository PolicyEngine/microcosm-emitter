"""The service owns lifecycle interpretation, sanitization, and atomic persistence."""

import json
import os
import time
from unittest.mock import Mock

import pytest
from microcosm_provider_client.contracts import ModuleContext
from microcosm_provider_telemetry.client import TelemetryRun
from microcosm_provider_telemetry.service.module import TelemetryModule
from sqlalchemy import event as sqlalchemy_event


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
    assert details["HF_TOKEN"] == "[redacted]" and details["traceback"] == "[redacted]"
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
    assert event["event_type"] == "heartbeat" and event["stage_id"] == "compile"
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


def test_module_close_disposes_database_when_delivery_fails(module):
    module.delivery.flush_once.side_effect = OSError("network")
    module.spool.close = Mock(wraps=module.spool.close)
    with pytest.raises(OSError):
        module.close(time.monotonic() + 1)
    module.spool.close.assert_called_once()


def test_failed_commit_preserves_active_stage_and_allows_completion_retry(module):
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    before = events(module)

    def fail_commit(session):
        session.flush()
        raise OSError("simulated commit failure")

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


def test_heartbeat_redacts_stage_identifiers(module):
    module.handle_message({"command": "stage", "stage_id": "hf_private_stage"})
    module.tick(time.monotonic() + 120)
    assert events(module)[-1]["stage_id"] == "[redacted]"
    assert "hf_private_stage" not in json.dumps(events(module))


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

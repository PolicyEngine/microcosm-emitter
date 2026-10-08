"""Service callbacks retain build outcomes and commit before returning."""

import os
import time
from unittest.mock import Mock

import pytest
from policyengine_local_service.contracts import ModuleContext
from policyengine_telemetry.client import TelemetryRun
from policyengine_telemetry.service.module import TelemetryModule


@pytest.fixture
def module(tmp_path):
    run = TelemetryRun("test-run", "UK", "test", producer_id="test-producer")
    module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "heartbeat_seconds": 1,
            "development_collector_url": "http://127.0.0.1:1",
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    module.delivery = Mock()
    yield module
    module.close(time.monotonic())


def events(module):
    return module.spool.batch("test-run", "test-producer")


def test_parent_exit_records_active_stage_once(module):
    module.handle_message(
        {"event_type": "stage", "status": "started", "stage_id": "compile"}
    )
    module.parent_exited()
    module.parent_exited()
    assert len(events(module)) == 2
    assert events(module)[-1]["details"]["failed_during"] == "compile"
    assert events(module)[-1]["status"] == "failed"


def test_reported_completion_prevents_later_failure_or_heartbeat(module):
    module.handle_message(
        {"event_type": "run", "status": "completed", "stage_id": "complete"}
    )
    module.parent_exited()
    module.tick(time.monotonic() + 120)
    assert len(events(module)) == 1


def test_heartbeat_is_persisted_with_resources(module):
    module.tick(time.monotonic() + 120)
    assert events(module)[0]["event_type"] == "heartbeat"
    assert events(module)[0]["resources"]["rss_bytes"] > 0


def test_persistence_error_propagates_so_runtime_rejects_ack(module, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(module.spool, "append", fail)
    with pytest.raises(OSError):
        module.handle_message({"event_type": "run", "status": "completed"})
    assert not module._finished


def test_module_close_disposes_database_when_delivery_fails(module):
    module.handle_message({"event_type": "run", "status": "started"})
    module.delivery.flush_once.side_effect = OSError("network")
    module.spool.close = Mock(wraps=module.spool.close)
    with pytest.raises(OSError):
        module.close(time.monotonic() + 1)
    module.spool.close.assert_called_once()


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"event_type": "bad", "status": "started"},
        {"event_type": "run", "status": "bad"},
    ],
)
def test_invalid_event_never_enters_queue(module, message):
    with pytest.raises(ValueError):
        module.handle_message(message)
    assert not events(module)

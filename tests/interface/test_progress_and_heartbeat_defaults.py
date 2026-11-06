"""Progress may carry an outer counter, and heartbeats default to every 30 seconds."""

from microcosm_emitter.core import constants as emitter_constants
from microcosm_emitter_interface import telemetry
from microcosm_emitter_interface.telemetry import (
    OuterProgress,
    StageProgress,
    TelemetryRun,
    TelemetrySettings,
    telemetry_message,
)


def test_progress_without_an_outer_counter_sends_no_outer_key():
    message = telemetry_message(StageProgress("score", 74, 177, "scoring_batch"))
    assert "outer" not in message


def test_progress_sends_its_outer_counter():
    message = telemetry_message(
        StageProgress(
            "score", 74, 177, "scoring_batch", outer=OuterProgress(57, 101, "pass")
        )
    )
    assert message.get("outer") == {"done": 57, "total": 101, "unit": "pass"}


def test_heartbeats_default_to_every_30_seconds(tmp_path):
    settings = TelemetrySettings(run=TelemetryRun("run", "US", "pipeline"))
    assert (
        settings.configuration(tmp_path / "events.sqlite3")["heartbeat_seconds"] == 30.0
    )
    assert telemetry.DEFAULT_HEARTBEAT_SECONDS == 30.0
    assert emitter_constants.DEFAULT_HEARTBEAT_SECONDS == 30.0

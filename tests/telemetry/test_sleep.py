"""A gap between a clock that stops during sleep and one that doesn't is a sleep."""

import os
import sys
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.clocks import (
    ClockReading,
    SleepDetector,
    SystemClocks,
    clock_ids,
)
from microcosm_emitter.telemetry.service.module import TelemetryModule

START = datetime(2026, 10, 24, 8, 31, tzinfo=UTC)


def clock(awake, asleep=0.0):
    """Return a reading ``awake`` seconds in, ``asleep`` seconds slept so far."""
    return ClockReading(
        awake=awake,
        total=awake + asleep,
        wall=START + timedelta(seconds=awake + asleep),
    )


class FakeClocks:
    """Clocks that return the given readings in order."""

    def __init__(self, readings):
        self.readings = list(readings)

    def read(self):
        return self.readings.pop(0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS clocks")
def test_macos_compares_the_uptime_clock_with_the_monotonic_clock():
    assert clock_ids("darwin") == (time.CLOCK_UPTIME_RAW, time.CLOCK_MONOTONIC)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux clocks")
def test_linux_compares_the_monotonic_clock_with_the_boot_clock():
    assert clock_ids("linux") == (time.CLOCK_MONOTONIC, time.CLOCK_BOOTTIME)


def test_other_platforms_have_no_sleep_clocks():
    assert clock_ids("win32") is None


def test_the_system_clocks_read_the_running_clock_at_or_ahead_of_the_awake_one():
    reading = SystemClocks().read()
    assert reading is not None
    assert reading.total >= reading.awake
    assert reading.wall.tzinfo is not None


def test_a_gap_between_the_clocks_is_a_sleep():
    detector = SleepDetector()
    assert detector.observe(clock(0)) is None
    assert detector.observe(clock(1)) is None
    sleep = detector.observe(clock(2, asleep=3600))
    assert sleep is not None
    assert sleep.seconds == 3600
    assert sleep.started_at == START + timedelta(seconds=1.5)
    assert sleep.ended_at == START + timedelta(seconds=3601.5)
    assert sleep.as_details() == {
        "started_at": "2026-10-24T08:31:01.500000+00:00",
        "ended_at": "2026-10-24T09:31:01.500000+00:00",
        "seconds": 3600.0,
    }


def test_a_gap_shorter_than_five_seconds_is_not_a_sleep():
    detector = SleepDetector()
    detector.observe(clock(0))
    assert detector.observe(clock(1, asleep=4.9)) is None
    assert detector.observe(clock(2, asleep=4.9)) is None


def test_without_clock_readings_there_is_no_sleep():
    detector = SleepDetector()
    assert detector.observe(None) is None
    assert detector.observe(clock(0)) is None
    assert detector.observe(None) is None
    assert detector.observe(clock(10, asleep=600)) is None


@pytest.fixture
def module(tmp_path):
    run = TelemetryRun("sleep-run", "US", "test", producer_id="sleep-producer")
    module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "heartbeat_seconds": 30,
            "development_collector_url": "http://127.0.0.1:1",
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    module.delivery = Mock()
    yield module
    module.close(time.monotonic())


def heartbeats(module):
    return [
        event
        for event in module.spool.batch("sleep-run", "sleep-producer")
        if event["event_type"] == "heartbeat"
    ]


def test_waking_sends_a_heartbeat_at_once_with_the_sleep(module):
    module.clocks = FakeClocks(
        [clock(0), clock(1, asleep=1800), clock(2, asleep=1800), clock(3, asleep=1800)]
    )
    now = time.monotonic()
    module.tick(now)
    assert heartbeats(module) == []
    module.tick(now + 1)
    recorded = heartbeats(module)
    assert len(recorded) == 1
    assert (recorded[0]["details"].get("sleep") or {}).get("seconds") == 1800.0
    assert "reading" in recorded[0]["details"]
    # The heartbeat after waking restarts the schedule: the next one is due a
    # full interval later.
    module.tick(now + 2)
    module.tick(now + 3)
    assert len(heartbeats(module)) == 1


def test_a_due_heartbeat_has_no_sleep(module):
    module.clocks = FakeClocks([clock(0), clock(1)])
    now = time.monotonic()
    module.tick(now)
    module.tick(now + 60)
    recorded = heartbeats(module)
    assert len(recorded) == 1
    assert "sleep" not in recorded[0]["details"]
    assert "reading" in recorded[0]["details"]

"""Every heartbeat carries a reading of the build's processes and of the machine."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service import resources as resources_module
from microcosm_emitter.telemetry.service.module import TelemetryModule
from microcosm_emitter.telemetry.service.readings import MachineReader
from microcosm_emitter.telemetry.service.resources import ProcessTreeSampler

MIB = 2**20
READING_KEYS = {
    "interval_seconds",
    "late_seconds",
    "stages",
    "progress",
    "process_tree",
    "machine",
}
PROCESS_TREE_KEYS = {
    "processes",
    "memory_bytes",
    "memory_peak_bytes",
    "interval_memory_peak_bytes",
    "cpu_user_cores",
    "cpu_system_cores",
    "disk_read_bytes",
    "disk_written_bytes",
}
MACHINE_KEYS = {
    "memory_available_bytes",
    "memory_compressed_bytes",
    "memory_pressure_level",
    "memory_stall_some_share",
    "memory_stall_full_share",
    "swap_used_bytes",
    "swap_total_bytes",
    "swap_in_bytes",
    "swap_out_bytes",
    "cpu_busy_share",
    "load_average_1m",
    "on_battery",
    "disk_free_bytes",
    "cgroup_memory_bytes",
    "cgroup_memory_limit_bytes",
}
# Allocates and touches 300 MiB, says so, then waits for its standard input to
# close.
ALLOCATING_CHILD = (
    "import sys\n"
    "block = bytearray(300 * 2**20)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)
# Uses about a second of CPU time, says so, then waits for its standard input
# to close.
BUSY_CHILD = (
    "import sys, time\n"
    "end = time.process_time() + 1.0\n"
    "while time.process_time() < end:\n"
    "    pass\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def module(tmp_path):
    run = TelemetryRun("reading-run", "US", "test", producer_id="reading-producer")
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
        for event in module.spool.batch("reading-run", "reading-producer")
        if event["event_type"] == "heartbeat"
    ]


def start_child(source):
    """Start a Python child running ``source`` and wait until it prints ready."""
    child = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "ready"
    return child


def stop_child(child):
    child.stdin.close()
    child.wait(timeout=30)
    child.stdout.close()


def test_each_heartbeat_carries_a_reading(module):
    module.handle_message(
        {"command": "transition_stage", "stage_id": "compile", "status": "running"}
    )
    module.handle_message(
        {
            "command": "progress",
            "stage_id": "compile",
            "done": 2,
            "total": 4,
            "unit": "batches",
            "outer": {"done": 1, "total": 3, "unit": "pass"},
        }
    )
    progress_event = module.spool.batch("reading-run", "reading-producer")[-1]
    module.tick(time.monotonic() + 60)

    heartbeat = heartbeats(module)[-1]
    reading = heartbeat["details"].get("reading") or {}
    assert set(reading) == READING_KEYS
    assert heartbeat["stage_id"] == "compile"
    assert reading["interval_seconds"] > 0
    assert reading["late_seconds"] >= 0
    assert reading["stages"] == ["compile"]
    assert reading["progress"] == {
        "stage_id": "compile",
        "done": 2,
        "total": 4,
        "unit": "batches",
        "outer": {"done": 1, "total": 3, "unit": "pass"},
        "reported_at": progress_event["timestamp"],
    }
    assert set(reading["process_tree"]) == PROCESS_TREE_KEYS
    assert reading["process_tree"]["processes"] >= 1
    assert reading["process_tree"]["memory_bytes"] > 0
    assert set(reading["machine"]) == MACHINE_KEYS
    assert heartbeat["resources"]["rss_bytes"] > 0


def test_a_reading_leaves_out_the_progress_of_a_stage_that_ended(module):
    module.handle_message({"command": "transition_stage", "stage_id": "load"})
    module.handle_message(
        {"command": "progress", "stage_id": "load", "done": 1, "total": 1}
    )
    module.handle_message({"command": "transition_stage", "stage_id": "compile"})
    module.tick(time.monotonic() + 60)
    reading = heartbeats(module)[-1]["details"].get("reading") or {}
    assert reading.get("stages") == ["compile"]
    assert "progress" in reading
    assert reading["progress"] is None


def test_a_heartbeat_outside_any_stage_names_the_last_stage(module):
    module.handle_message({"command": "transition_stage", "stage_id": "load"})
    module.handle_message(
        {"command": "transition_stage", "stage_id": "load", "status": "completed"}
    )
    module.tick(time.monotonic() + 60)
    heartbeat = heartbeats(module)[-1]
    assert heartbeat["stage_id"] == "load"
    assert (heartbeat["details"].get("reading") or {}).get("stages") == []


def test_the_reading_counts_the_memory_of_child_processes(module):
    module.tick(time.monotonic() + 60)
    before = (heartbeats(module)[-1]["details"].get("reading") or {}).get(
        "process_tree", {}
    )
    child = start_child(ALLOCATING_CHILD)
    try:
        module.tick(time.monotonic() + 120)
    finally:
        stop_child(child)
    tree = (heartbeats(module)[-1]["details"].get("reading") or {}).get(
        "process_tree", {}
    )
    assert tree.get("processes", 0) >= before.get("processes", 0) + 1
    assert tree.get("memory_bytes", 0) >= before.get("memory_bytes", 0) + 250 * MIB
    assert tree["memory_peak_bytes"] >= tree["memory_bytes"]
    assert tree["interval_memory_peak_bytes"] >= tree["memory_bytes"]


def test_an_interval_peak_starts_again_after_each_reading():
    sampler = ProcessTreeSampler(os.getpid())
    child = start_child(ALLOCATING_CHILD)
    try:
        sampler.sample()
    finally:
        stop_child(child)
    first = sampler.tree_reading(1.0)
    sampler.sample()
    second = sampler.tree_reading(1.0)
    assert first.get("interval_memory_peak_bytes", 0) >= 250 * MIB
    assert second.get("interval_memory_peak_bytes", 0) < first.get(
        "interval_memory_peak_bytes", 0
    )
    assert second.get("memory_peak_bytes") == first.get("memory_peak_bytes")


def test_a_process_that_exits_keeps_its_cpu_time():
    sampler = ProcessTreeSampler(os.getpid())
    sampler.sample()
    sampler.tree_reading(1.0)
    child = start_child(BUSY_CHILD)
    sampler.sample()
    stop_child(child)
    sampler.sample()
    reading = sampler.tree_reading(1.0)
    assert reading.get("cpu_user_cores", 0) + reading.get("cpu_system_cores", 0) >= 0.5
    sampler.sample()
    after = sampler.tree_reading(1.0)
    assert after.get("cpu_user_cores", -1) >= 0
    assert after.get("cpu_system_cores", -1) >= 0


def test_machine_readings_on_this_platform():
    reader = MachineReader(os.getpid())
    reader.reading()
    reading = reader.reading()
    assert set(reading) == MACHINE_KEYS
    assert reading["memory_available_bytes"] > 0
    assert reading["swap_total_bytes"] >= 0
    assert reading["swap_used_bytes"] >= 0
    assert reading["cpu_busy_share"] is None or 0 <= reading["cpu_busy_share"] <= 1
    assert reading["load_average_1m"] >= 0
    assert reading["disk_free_bytes"] > 0
    assert reading["on_battery"] in {None, True, False}
    if sys.platform == "darwin":
        assert reading["memory_pressure_level"] in {"normal", "warning", "critical"}
        assert reading["memory_compressed_bytes"] >= 0
        # macOS's page-in and page-out counters count every kind of paging,
        # not only swap, so they are not reported as swap.
        assert (reading["swap_in_bytes"], reading["swap_out_bytes"]) == (None, None)
    else:
        assert reading["memory_pressure_level"] is None
        assert reading["memory_compressed_bytes"] is None
        assert reading["swap_in_bytes"] >= 0
        assert reading["swap_out_bytes"] >= 0


def test_a_wall_clock_step_does_not_count_the_processes_again(monkeypatch):
    """Count a process once across a wall-clock step.

    The fake identifies a process as psutil does, by its PID and its start
    time since boot, while its ``create_time()`` follows the wall clock and so
    moves when the clock is stepped.
    """
    clock = {"offset": 0.0}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def __eq__(self, other):
            return isinstance(other, FakeProcess) and other.pid == self.pid

        def __hash__(self):
            return hash(self.pid)

        def create_time(self):
            return 1_000.0 + self.pid + clock["offset"]

        def children(self, *, recursive=False):
            return [FakeProcess(11)] if self.pid == 10 else []

        def cpu_times(self):
            return SimpleNamespace(
                user=3600.0, system=600.0, children_user=0.0, children_system=0.0
            )

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def io_counters(self):
            return SimpleNamespace(read_bytes=10**9, write_bytes=10**9)

    monkeypatch.setattr(
        resources_module,
        "psutil",
        SimpleNamespace(Process=FakeProcess, Error=OSError),
    )
    sampler = ProcessTreeSampler(10, system_platform="linux")
    sampler.sample()
    sampler.tree_reading(30.0)
    clock["offset"] = 3600.0
    sampler.sample()
    reading = sampler.tree_reading(30.0)
    assert (reading["cpu_user_cores"], reading["cpu_system_cores"]) == (0.0, 0.0)
    assert (reading["disk_read_bytes"], reading["disk_written_bytes"]) == (0, 0)


def test_progress_sent_through_emit_inside_an_open_stage_is_in_the_reading(module):
    """Microcosm's UK builds send graph-node progress through ``emit``."""
    module.handle_message({"command": "transition_stage", "stage_id": "uk_build"})
    module.handle_message(
        {
            "command": "emit",
            "event_type": "progress",
            "stage_id": "node.population",
            "status": "completed",
            "details": {"done": 3, "total": 40, "unit": "graph_nodes"},
        }
    )
    progress_event = module.spool.batch("reading-run", "reading-producer")[-1]
    module.tick(time.monotonic() + 60)
    reading = heartbeats(module)[-1]["details"].get("reading") or {}
    assert reading.get("stages") == ["uk_build"]
    assert reading.get("progress") == {
        "stage_id": "node.population",
        "done": 3,
        "total": 40,
        "unit": "graph_nodes",
        "outer": None,
        "reported_at": progress_event["timestamp"],
    }
    module.handle_message(
        {"command": "transition_stage", "stage_id": "uk_build", "status": "completed"}
    )
    module.handle_message({"command": "transition_stage", "stage_id": "export"})
    module.tick(time.monotonic() + 120)
    assert (heartbeats(module)[-1]["details"].get("reading") or {}).get(
        "progress", "missing"
    ) is None


def test_a_reading_at_its_largest_stays_within_the_details_limit(module):
    for index in range(32):
        module.handle_message(
            {"command": "stage", "stage_id": f"{index:02d}" + "s" * 190}
        )
    module.handle_message(
        {
            "command": "progress",
            "stage_id": "31" + "s" * 190,
            "done": "d" * 2000,
            "total": "t" * 2000,
            "unit": "u" * 2000,
            "outer": {"done": 1, "total": 2, "unit": "p" * 200},
        }
    )
    module.clocks = FakeSleepClocks()
    module.tick(time.monotonic())
    module.tick(time.monotonic() + 60)
    details = heartbeats(module)[-1]["details"]
    assert "reading" in details
    assert "sleep" in details
    assert len(details["reading"]["stages"]) == 32
    progress = details["reading"]["progress"] or {}
    assert (progress.get("done"), progress.get("total")) == (None, None)
    assert len(progress.get("unit") or "") <= 200
    assert len(json.dumps(details, separators=(",", ":")).encode()) <= 8192


class FakeSleepClocks:
    """Clocks across which the machine slept for an hour."""

    def __init__(self):
        from datetime import UTC, datetime

        from microcosm_emitter.telemetry.service.clocks import ClockReading

        start = datetime(2026, 11, 6, 12, tzinfo=UTC)
        self.readings = [
            ClockReading(0.0, 0.0, start),
            ClockReading(1.0, 3601.0, start),
        ]

    def read(self):
        return self.readings.pop(0) if self.readings else None


def test_wide_characters_and_long_numbers_keep_a_reading_within_the_limit(module):
    stage_ids = [f"{index:02d}" + "s" * 190 for index in range(32)]
    for stage_id in stage_ids:
        module.handle_message({"command": "stage", "stage_id": stage_id})
    module.handle_message(
        {
            "command": "progress",
            "stage_id": stage_ids[-1],
            "done": 10**400,
            "total": 1.5,
            "unit": "é" * 2000,
            "outer": {"done": 1, "total": 2, "unit": "\U0001f600" * 200},
        }
    )
    module.clocks = FakeSleepClocks()
    module.tick(time.monotonic())
    module.tick(time.monotonic() + 60)
    details = heartbeats(module)[-1]["details"]
    assert len(json.dumps(details, separators=(",", ":")).encode()) <= 8192
    reading = details["reading"]
    assert "sleep" in details
    # Only the outermost stages may be left out, to keep the reading in bounds.
    assert reading["stages"] == stage_ids[len(stage_ids) - len(reading["stages"]) :]
    progress = reading["progress"] or {}
    assert (progress.get("done"), progress.get("total")) == (None, 1.5)
    assert progress.get("unit", "").startswith("é")


def fake_psutil(processes):
    """Return a psutil stand-in over ``processes``: process ID to its state.

    Each state holds ``start`` (its start time since boot, which psutil uses
    to tell processes apart), ``user`` CPU seconds and ``disk``, the bytes
    read so far, or None when reading them fails.
    """

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self.start = processes[pid]["start"]

        def __eq__(self, other):
            return isinstance(other, FakeProcess) and (other.pid, other.start) == (
                self.pid,
                self.start,
            )

        def __hash__(self):
            return hash((self.pid, self.start))

        def children(self, *, recursive=False):
            return [FakeProcess(pid) for pid in processes if pid != self.pid]

        def cpu_times(self):
            return SimpleNamespace(
                user=processes[self.pid]["user"],
                system=0.0,
                children_user=0.0,
                children_system=0.0,
            )

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def io_counters(self):
            disk = processes[self.pid]["disk"]
            if disk is None:
                raise OSError(3, "No such process")
            return SimpleNamespace(read_bytes=disk, write_bytes=0)

    return SimpleNamespace(Process=FakeProcess, Error=OSError)


def test_a_disk_read_that_fails_once_keeps_the_process_disk_totals(monkeypatch):
    processes = {
        10: {"start": 1.0, "user": 0.0, "disk": 0},
        11: {"start": 2.0, "user": 0.0, "disk": 1000},
    }
    monkeypatch.setattr(resources_module, "psutil", fake_psutil(processes))
    sampler = ProcessTreeSampler(10, system_platform="linux")
    sampler.sample()
    sampler.tree_reading(30.0)
    processes[11]["disk"] = None
    sampler.sample()
    assert sampler.tree_reading(30.0)["disk_read_bytes"] == 0
    processes[11]["disk"] = 1100
    sampler.sample()
    assert sampler.tree_reading(30.0)["disk_read_bytes"] == 100


def test_a_reused_process_id_counts_the_new_process_from_its_start(monkeypatch):
    processes = {
        10: {"start": 1.0, "user": 0.0, "disk": 0},
        11: {"start": 2.0, "user": 100.0, "disk": 0},
    }
    monkeypatch.setattr(resources_module, "psutil", fake_psutil(processes))
    sampler = ProcessTreeSampler(10, system_platform="linux")
    sampler.sample()
    sampler.tree_reading(10.0)
    processes[11] = {"start": 50.0, "user": 5.0, "disk": 0}
    sampler.sample()
    assert sampler.tree_reading(10.0)["cpu_user_cores"] == 0.5

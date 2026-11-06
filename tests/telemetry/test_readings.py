"""Every heartbeat carries a reading of the build's processes and of the machine."""

import os
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
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

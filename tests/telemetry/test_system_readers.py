"""Readings psutil doesn't provide: macOS footprints and sysctls, Linux cgroups."""

import subprocess
import sys

import pytest

from microcosm_emitter.telemetry.service.system_readers import (
    CgroupReadings,
    cgroup_directory,
    macos_process_usage,
    parse_cpu_max,
    parse_memory_max,
    parse_pressure,
    read_cgroup,
    read_pressure,
    sysctl_number,
    sysctl_text,
)

MIB = 2**20
PRESSURE = (
    "some avg10=12.50 avg60=3.00 avg300=1.00 total=123456\n"
    "full avg10=0.25 avg60=0.10 avg300=0.00 total=789\n"
)


def test_pressure_stall_shares_are_the_ten_second_averages():
    assert parse_pressure(PRESSURE) == (0.125, 0.0025)


@pytest.mark.parametrize(
    "text",
    ["", "some avg60=1.00\n", "some avg10=high\nfull avg10=\n", "nonsense"],
)
def test_unreadable_pressure_is_unknown(text):
    assert parse_pressure(text) == (None, None)


@pytest.mark.parametrize(
    ("text", "cores"),
    [("max 100000\n", None), ("200000 100000\n", 2.0), ("50000 100000", 0.5)],
)
def test_a_cpu_limit_is_its_quota_over_its_period(text, cores):
    assert parse_cpu_max(text) == cores


@pytest.mark.parametrize("text", ["", "max", "200000 0", "a b", "1 2 3"])
def test_an_unreadable_or_unlimited_cpu_limit_is_none(text):
    assert parse_cpu_max(text) is None


@pytest.mark.parametrize(
    ("text", "limit"),
    [("max\n", None), ("1073741824\n", 1073741824), ("", None), ("lots", None)],
)
def test_a_memory_limit_is_a_byte_count_or_none(text, limit):
    assert parse_memory_max(text) == limit


def cgroup_tree(tmp_path, membership, *, controllers=True):
    """Write a fake /proc/self/cgroup naming ``membership``, and a cgroup root."""
    proc_file = tmp_path / "proc-self-cgroup"
    proc_file.write_text(membership)
    root = tmp_path / "cgroup"
    directory = root / "modal" / "task"
    directory.mkdir(parents=True)
    if controllers:
        (directory / "cgroup.controllers").write_text("cpu memory\n")
    return proc_file, root, directory


def test_the_cgroup_directory_comes_from_the_unified_hierarchy_entry(tmp_path):
    proc_file, root, directory = cgroup_tree(
        tmp_path, "12:memory:/legacy\n0::/modal/task\n"
    )
    assert cgroup_directory(proc_file, root) == directory


@pytest.mark.parametrize("membership", ["12:memory:/modal/task\n", "", "0::/missing\n"])
def test_without_a_unified_cgroup_there_is_no_directory(tmp_path, membership):
    proc_file, root, _ = cgroup_tree(tmp_path, membership)
    assert cgroup_directory(proc_file, root) is None


def test_a_directory_without_controllers_is_not_a_cgroup(tmp_path):
    proc_file, root, _ = cgroup_tree(tmp_path, "0::/modal/task\n", controllers=False)
    assert cgroup_directory(proc_file, root) is None


def test_a_missing_membership_file_means_no_cgroup(tmp_path):
    assert cgroup_directory(tmp_path / "absent", tmp_path) is None


def test_cgroup_readings_come_from_its_files(tmp_path):
    _, _, directory = cgroup_tree(tmp_path, "0::/modal/task\n")
    (directory / "memory.current").write_text("1048576\n")
    (directory / "memory.max").write_text("4294967296\n")
    (directory / "cpu.max").write_text("400000 100000\n")
    (directory / "memory.pressure").write_text(PRESSURE)
    assert read_cgroup(directory) == CgroupReadings(
        memory_bytes=1048576,
        memory_limit_bytes=4294967296,
        cpu_limit_cores=4.0,
        memory_stall_some_share=0.125,
        memory_stall_full_share=0.0025,
    )


def test_a_cgroup_without_its_files_reads_as_unknown(tmp_path):
    _, _, directory = cgroup_tree(tmp_path, "0::/modal/task\n")
    assert read_cgroup(directory) == CgroupReadings()
    assert read_cgroup(None) == CgroupReadings()


def test_system_pressure_comes_from_the_given_file(tmp_path):
    pressure = tmp_path / "memory"
    pressure.write_text(PRESSURE)
    assert read_pressure(pressure) == (0.125, 0.0025)
    assert read_pressure(tmp_path / "absent") == (None, None)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS readings")
def test_macos_reads_a_childs_physical_footprint():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\nblock = bytearray(300 * 2**20)\n"
                "print('ready', flush=True)\nsys.stdin.read()\n"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        usage = macos_process_usage(child.pid)
    finally:
        child.stdin.close()
        child.wait(timeout=30)
        child.stdout.close()
    assert usage is not None
    assert usage.memory_bytes >= 250 * MIB
    assert usage.disk_read_bytes >= 0
    assert usage.disk_written_bytes >= 0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS readings")
def test_macos_sysctls_give_the_pressure_level_and_cpu_model():
    assert sysctl_number("kern.memorystatus_vm_pressure_level") in {1, 2, 4}
    assert sysctl_number("hw.memsize") > 0
    assert sysctl_number("no.such.sysctl") is None
    model = sysctl_text("machdep.cpu.brand_string")
    assert isinstance(model, str)
    assert model
    assert sysctl_text("no.such.sysctl") is None


@pytest.mark.skipif(sys.platform == "darwin", reason="readings outside macOS")
def test_macos_readings_are_unavailable_elsewhere():
    assert macos_process_usage(1) is None
    assert sysctl_number("hw.memsize") is None
    assert sysctl_text("machdep.cpu.brand_string") is None

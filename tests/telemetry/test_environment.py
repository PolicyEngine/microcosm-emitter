"""The run-started event says where the build runs, and never names the machine."""

import json
import os
import platform
import time
from importlib.metadata import version
from unittest.mock import Mock

import psutil

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.environment import machine_environment
from microcosm_emitter.telemetry.service.module import TelemetryModule

ENVIRONMENT_KEYS = {
    "kind",
    "os",
    "os_version",
    "architecture",
    "cpu_model",
    "logical_cpus",
    "memory_total_bytes",
    "cgroup_cpu_limit_cores",
    "cgroup_memory_limit_bytes",
    "modal",
    "emitter_version",
    "python_version",
}
MODAL_VARIABLES = {
    "MODAL_TASK_ID": "ta-fixture-task",
    "MODAL_REGION": "us-east",
    "MODAL_CLOUD_PROVIDER": "CLOUD_PROVIDER_AWS",
    "MODAL_IS_REMOTE": "1",
    "MODAL_IDENTITY_TOKEN": "fixture-identity-credential",
    "MODAL_ENVIRONMENT": "fixture-modal-environment",
    "MODAL_IMAGE_ID": "im-fixture-image",
}


def test_a_local_machine_is_described_without_its_name():
    environment = machine_environment({})
    assert set(environment) == ENVIRONMENT_KEYS
    assert environment["kind"] == "local"
    assert environment["modal"] is None
    assert environment["os"] == platform.system()
    assert environment["architecture"] == platform.machine()
    assert environment["logical_cpus"] == os.cpu_count()
    assert environment["memory_total_bytes"] == psutil.virtual_memory().total
    assert environment["python_version"] == platform.python_version()
    assert environment["emitter_version"] == version("microcosm-emitter")
    assert platform.node() not in json.dumps(environment)


def test_a_modal_container_is_described_by_its_task_region_and_provider():
    environment = machine_environment(MODAL_VARIABLES)
    assert environment.get("kind") == "modal_container"
    assert environment.get("modal") == {
        "task_id": "ta-fixture-task",
        "region": "us-east",
        "cloud_provider": "CLOUD_PROVIDER_AWS",
    }
    text = json.dumps(environment)
    for name in ("MODAL_IDENTITY_TOKEN", "MODAL_ENVIRONMENT", "MODAL_IMAGE_ID"):
        assert MODAL_VARIABLES[name] not in text


def test_a_blank_task_id_is_a_local_machine():
    environment = machine_environment({"MODAL_TASK_ID": "  "})
    assert environment.get("kind") == "local"
    assert "modal" in environment
    assert environment["modal"] is None


def test_the_run_started_event_carries_the_description_beside_the_identity(
    tmp_path, monkeypatch
):
    for name, value in MODAL_VARIABLES.items():
        monkeypatch.setenv(name, value)
    run = TelemetryRun("environment-run", "US", "test", producer_id="producer")
    module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "development_collector_url": "http://127.0.0.1:1",
            "identity": {"git_commit": "abc123"},
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    module.delivery = Mock()
    try:
        started = module.spool.batch("environment-run", "producer")[0]
    finally:
        module.close(time.monotonic())
    assert started["details"]["identity"] == {"git_commit": "abc123"}
    environment = started["details"].get("environment") or {}
    assert environment.get("kind") == "modal_container"
    assert (environment.get("modal") or {}).get("task_id") == "ta-fixture-task"

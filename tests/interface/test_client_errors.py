"""No interface call raises into the build, whatever arguments it is given.

Arguments are converted to JSON inside each call's error handling: a value that
cannot be converted makes the call report failure, with a reason, both with and
without a running emitter.
"""

from pathlib import Path
from typing import Any

import pytest

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings


class MultiElementArray:
    """Behaves like a NumPy array or tensor with more than one element."""

    def item(self):
        error_message = "can only convert an array of size 1 to a Python scalar"
        raise ValueError(error_message)


def self_referencing_mapping() -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    mapping["itself"] = mapping
    return mapping


def deeply_nested_list(depth: int = 100_000) -> list[Any]:
    nested: list[Any] = []
    for _ in range(depth):
        nested = [nested]
    return nested


class UnresolvablePath:
    """A path-like argument whose conversion fails."""

    def __fspath__(self):
        error_message = "synthetic unresolvable path"
        raise OSError(error_message)


class RecordingTransport:
    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, module, message, *, timeout=None):
        self.requests.append(("message", module, dict(message)))

    def query(self, module, message, *, timeout=None):
        self.requests.append(("query", module, dict(message)))
        return {}

    def close(self):
        pass


def running_client() -> tuple[EmitterClient, RecordingTransport]:
    transport = RecordingTransport()
    process = EmitterProcess(
        process=None,
        transport=transport,
        runtime_directory=Path("/nonexistent"),
        description={"modules": ["orrery", "telemetry"], "protocol_versions": [1]},
    )
    return EmitterClient(process), transport


UNCONVERTIBLE_DETAILS = {
    "multi-element array": MultiElementArray,
    "self-referencing mapping": self_referencing_mapping,
    "deep nesting": deeply_nested_list,
}


@pytest.mark.parametrize(
    "make_value", UNCONVERTIBLE_DETAILS.values(), ids=list(UNCONVERTIBLE_DETAILS)
)
def test_unconvertible_telemetry_arguments_report_failure_with_an_emitter(
    make_value, capsys
):
    client, transport = running_client()
    assert client.telemetry.stage("compile", value=make_value()) is False
    assert client.telemetry.calibration_progress({"loss": make_value()}) is False
    assert transport.requests == []
    from microcosm_emitter_interface.client import RequestFailure

    failure = client.last_failure
    assert isinstance(failure, RequestFailure)
    assert (failure.kind, failure.reason) == ("telemetry", "invalid_arguments")
    assert failure.error_type in {"ValueError", "RecursionError"}
    warning = capsys.readouterr().err
    assert warning.count("did not accept a telemetry request") == 1
    assert "invalid_arguments" in warning
    assert "synthetic" not in warning
    assert "scalar" not in warning


@pytest.mark.parametrize(
    "make_value", UNCONVERTIBLE_DETAILS.values(), ids=list(UNCONVERTIBLE_DETAILS)
)
def test_unconvertible_telemetry_arguments_report_failure_without_an_emitter(
    make_value,
):
    client = EmitterClient()
    assert client.telemetry.stage("compile", value=make_value()) is False
    assert (
        client.telemetry.emit(
            event_type="progress", status="progress", details={"value": make_value()}
        )
        is False
    )
    assert client.last_failure is not None
    assert client.last_failure.reason == "emitter_unavailable"


def test_unconvertible_refusal_and_failure_arguments_report_failure():
    client, transport = running_client()
    assert (
        client.telemetry.refuse(
            phase="preflight", refusing_check_ids=MultiElementArray()
        )
        is False
    )
    assert (
        client.telemetry.fail(RuntimeError("x"), failed_during=MultiElementArray())
        is False
    )
    assert transport.requests == []


def test_an_emitter_that_raises_an_unexpected_error_is_a_failure_not_a_raise():
    client, transport = running_client()

    def unexpected(*args, **kwargs):
        error_message = "synthetic"
        raise RecursionError(error_message)

    transport.send = unexpected
    transport.query = unexpected
    assert client.telemetry.complete() is False
    assert client.last_failure.reason == "not_accepted"
    assert client.orrery_receipt("fixture-publication")["status"] == "failed"


def test_successful_call_after_a_failure_keeps_the_last_failure_until_the_next():
    client, transport = running_client()
    assert client.telemetry.stage("compile", value=MultiElementArray()) is False
    assert client.telemetry.complete() is True
    assert transport.requests[-1] == ("message", "telemetry", {"command": "complete"})


class ReceiptlessGraphTransport:
    """The in-process client's transport, answering without a receipt."""

    def request(self, message, *, timeout=None):
        return {"receipt": None}

    def close(self):
        pass


def test_an_orrery_receipt_reports_the_error_code_of_its_own_call():
    client, transport = running_client()
    transport.query = lambda module, message, *, timeout=None: {"receipt": None}
    assert client.telemetry.stage("compile", value=MultiElementArray()) is False
    assert client.last_failure.reason == "invalid_arguments"

    receipt = client.orrery_receipt("a-publication-this-spool-never-queued")

    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "local_service_unavailable"
    from microcosm_emitter.orrery.client import GraphPublicationClient

    in_process_client = GraphPublicationClient(ReceiptlessGraphTransport())
    assert receipt == in_process_client.receipt("a-publication-this-spool-never-queued")


def test_an_orrery_retry_reports_the_error_code_of_its_own_call():
    client, transport = running_client()
    transport.query = lambda module, message, *, timeout=None: {"receipt": None}
    assert client.orrery_inventory(UnresolvablePath(), {"graph.json": "graph"}) is None
    assert client.last_failure.reason == "invalid_arguments"

    receipt = client.retry_orrery_inputs("a-publication-this-spool-never-queued")

    assert receipt["error_code"] == "local_service_unavailable"


def test_an_orrery_receipt_without_a_status_is_an_unavailable_receipt():
    client, transport = running_client()
    transport.query = lambda module, message, *, timeout=None: {
        "receipt": {"publication_id": "fixture-publication"}
    }

    receipt = client.orrery_receipt("fixture-publication")
    retried = client.retry_orrery_inputs("fixture-publication", wait_seconds=1)
    handed_over = client.hand_over_orrery_inputs(
        Path("/tmp"),
        {"version": 1, "publication_id": "fixture-publication", "files": []},
        wait_seconds=1,
    )

    for answered in (receipt, retried, handed_over):
        assert answered["status"] == "failed"
        assert answered["error_code"] == "local_service_unavailable"


def test_unconvertible_orrery_arguments_report_failure():
    client, transport = running_client()
    assert client.orrery_inventory(UnresolvablePath(), {"graph.json": "graph"}) is None
    receipt = client.hand_over_orrery_inputs(
        UnresolvablePath(),
        {"version": 1, "publication_id": "fixture-publication", "files": []},
        wait_seconds=0,
    )
    assert receipt["status"] == "failed"
    assert receipt["publication_id"] == "fixture-publication"
    assert receipt["error_code"] == "invalid_arguments"
    assert client.last_failure.reason == "invalid_arguments"
    missing_identifier = client.hand_over_orrery_inputs(
        Path("/tmp"), {"version": 1, "files": []}, wait_seconds=0
    )
    assert missing_identifier["status"] == "failed"
    assert missing_identifier["publication_id"] is None
    assert all(
        request[2].get("command") != "inventory" for request in transport.requests
    )


def test_unconvertible_startup_settings_start_an_unavailable_client(tmp_path, capsys):
    settings = TelemetrySettings(
        run=TelemetryRun("bad-settings", "UK", "test"),
        identity={"accelerator": MultiElementArray()},
    )
    client = EmitterClient.start(
        telemetry=settings, spool_path=tmp_path / "events.sqlite3"
    )
    assert not client.available
    assert client.startup_error is not None
    assert client.startup_error.reason == "invalid_configuration"
    assert client.telemetry.complete() is False
    warning = capsys.readouterr().err
    assert "invalid_configuration" in warning
    assert "scalar" not in warning


def test_unconvertible_spool_path_starts_an_unavailable_client():
    client = EmitterClient.start(
        telemetry=TelemetrySettings(run=TelemetryRun("bad-path", "UK", "test")),
        spool_path=UnresolvablePath(),
    )
    assert not client.available
    assert client.startup_error.reason == "invalid_configuration"


def hugging_face_client() -> tuple[EmitterClient, RecordingTransport]:
    transport = RecordingTransport()
    process = EmitterProcess(
        process=None,
        transport=transport,
        runtime_directory=Path("/nonexistent"),
        description={"modules": ["huggingface"], "protocol_versions": [1]},
    )
    return EmitterClient(process), transport


@pytest.mark.parametrize("running", [True, False], ids=["running", "no-emitter"])
def test_unconvertible_staging_arguments_report_failure(running):
    from microcosm_emitter_interface.staging import StageRunFiles

    client, transport = hugging_face_client() if running else (EmitterClient(), None)
    request = StageRunFiles(
        repository="policyengine/fixture-staging",
        run_id="run-1",
        run_index_entry={"run_id": "run-1", "loss": MultiElementArray()},
    )
    receipt = client.stage_run_files(request)
    assert receipt["status"] == "unavailable"
    assert receipt["run_id"] == "run-1"
    invalid_run = client.run_files_receipt("policyengine/fixture-staging", "../run")
    assert invalid_run["status"] == "unavailable"
    read_back = client.read_back_run_files("no-owner", "run-1", wait_seconds=0)
    assert read_back["status"] == "unavailable"
    access = client.check_write_access("no-owner")
    assert access["status"] == "unavailable"
    if running:
        assert transport.requests == []
        assert client.last_failure.reason == "invalid_arguments"


@pytest.mark.parametrize("running", [True, False], ids=["running", "no-emitter"])
def test_unconvertible_bundle_arguments_report_failure(running, tmp_path):
    import hashlib

    from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle

    client, transport = hugging_face_client() if running else (EmitterClient(), None)

    def bundle_file(name, role, local_path):
        return BundleFile(
            name=name,
            role=role,
            local_path=local_path,
            sha256=hashlib.sha256(b"").hexdigest(),
            bytes=0,
        )

    request = StageDatasetBundle(
        repository="policyengine/fixture-staging",
        run_id="run-1",
        files=(
            bundle_file("dataset.h5", "dataset", UnresolvablePath()),
            bundle_file("staged_manifest.json", "staged_manifest", tmp_path / "m"),
        ),
    )
    receipt = client.stage_dataset_bundle(request, wait_seconds=0)
    assert receipt["status"] == "unavailable"
    assert receipt["repository"] == "policyengine/fixture-staging"
    assert receipt["prefix"] == "staged/run-1"
    if running:
        assert transport.requests == []
        assert client.last_failure.reason == "invalid_arguments"


def test_a_pending_bundle_receipt_without_an_identifier_is_returned_not_raised(
    tmp_path,
):
    import hashlib

    from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle

    client, transport = hugging_face_client()
    transport.query = lambda module, message, *, timeout=None: {
        "receipt": {"status": "pending"}
    }
    manifest = tmp_path / "staged_manifest.json"
    manifest.write_bytes(b"{}")
    request = StageDatasetBundle(
        repository="policyengine/fixture-staging",
        run_id="run-1",
        files=(
            BundleFile(
                name="staged_manifest.json",
                role="staged_manifest",
                local_path=manifest,
                sha256=hashlib.sha256(b"{}").hexdigest(),
                bytes=2,
            ),
            BundleFile(
                name="dataset.h5",
                role="dataset",
                local_path=manifest,
                sha256=hashlib.sha256(b"{}").hexdigest(),
                bytes=2,
            ),
        ),
    )

    receipt = client.stage_dataset_bundle(request, wait_seconds=1)

    assert receipt == {"status": "pending"}


@pytest.mark.parametrize("running", [True, False], ids=["running", "no-emitter"])
def test_unconvertible_release_arguments_report_failure(running):
    import hashlib

    from microcosm_emitter_interface.publication import PublishRelease, ReleaseFile

    client, transport = hugging_face_client() if running else (EmitterClient(), None)
    request = PublishRelease(
        repository="policyengine/fixture-private",
        release="fixture-release",
        build="fixture-build",
        release_role="national_default",
        make_current=True,
        files=(
            ReleaseFile(
                name="release_manifest.json",
                role="release_manifest",
                local_path=UnresolvablePath(),
                sha256=hashlib.sha256(b"").hexdigest(),
                bytes=0,
            ),
        ),
    )
    receipt = client.publish_release(request, wait_seconds=0)
    assert receipt["status"] == "unavailable"
    assert (receipt["release"], receipt["build"]) == (
        "fixture-release",
        "fixture-build",
    )
    if running:
        assert transport.requests == []
        assert client.last_failure.reason == "invalid_arguments"

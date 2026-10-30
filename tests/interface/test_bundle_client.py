"""Typed staged-dataset-bundle hand-overs, each file named with its role."""

import hashlib
import re
from pathlib import Path

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.identifiers import ROLE_IDENTIFIER
from microcosm_emitter_interface.transport import SocketTransport


def bundle_file(directory: Path, name: str, role: str, content: bytes) -> BundleFile:
    path = directory / name
    path.write_bytes(content)
    return BundleFile(
        name=name,
        role=role,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def minimal_bundle(directory: Path) -> tuple[BundleFile, ...]:
    return (
        bundle_file(directory, "dataset.h5", "dataset", b"rows"),
        bundle_file(
            directory, "calibration_diagnostics.json", "calibration_diagnostics", b"{}"
        ),
        bundle_file(
            directory,
            "staged_manifest.json",
            "staged_manifest",
            b'{"files": {"calibration_diagnostics.json": {"sha256": "x"}}}',
        ),
    )


def with_replaced(files, index, **changes):
    original = files[index]
    replaced = BundleFile(
        name=changes.get("name", original.name),
        role=changes.get("role", original.role),
        local_path=original.local_path,
        sha256=changes.get("sha256", original.sha256),
        bytes=changes.get("bytes", original.bytes),
    )
    return (*files[:index], replaced, *files[index + 1 :])


@pytest.mark.parametrize(
    ("build_files", "message"),
    [
        (lambda files: files[:2], "A bundle has exactly one staged_manifest file."),
        (lambda files: (*files, files[2]), "A bundle names each file once."),
        (
            lambda files: with_replaced(files, 0, name="folder/dataset.h5"),
            "bundle file name must be a bare file name.",
        ),
        (
            lambda files: with_replaced(files, 0, role="Dataset"),
            f"role must match {ROLE_IDENTIFIER.pattern}.",
        ),
        (
            lambda files: with_replaced(files, 0, sha256="not-a-digest"),
            "sha256 must be a lowercase hexadecimal digest.",
        ),
        (
            lambda files: with_replaced(files, 0, bytes=-1),
            "bytes must be a nonnegative integer.",
        ),
        (lambda files: files[2:], "A bundle has at least one output file."),
        (
            lambda files: with_replaced(files, 1, name="dataset.h5"),
            "A bundle names each file once.",
        ),
    ],
    ids=[
        "no-inventory",
        "two-inventories",
        "folder-in-name",
        "role",
        "digest",
        "size",
        "no-output",
        "duplicate-name",
    ],
)
def test_bundles_that_break_the_rules_cannot_be_built(tmp_path, build_files, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        StageDatasetBundle(
            repository=PRIVATE_REPOSITORY,
            run_id="uk-run-1",
            files=build_files(minimal_bundle(tmp_path)),
        )


def test_hand_over_identity_follows_the_request_content(tmp_path):
    files = minimal_bundle(tmp_path)
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id="uk-run-1", files=files
    )
    same = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id="uk-run-1", files=files
    )
    changed = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id="uk-run-1",
        files=with_replaced(files, 0, sha256="0" * 64),
    )
    assert request.handover_id() == same.handover_id()
    assert request.handover_id() != changed.handover_id()
    assert len(request.handover_id()) == 32


def test_message_names_each_file_role_and_digest(tmp_path):
    files = minimal_bundle(tmp_path)
    message = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id="uk-run-1",
        files=files,
        commit_message="Stage the fixture",
    ).message()
    assert message["command"] == "stage_dataset_bundle"
    assert message["prefix"] == "staged"
    assert message["commit_message"] == "Stage the fixture"
    assert message["files"][1] == {
        "name": "calibration_diagnostics.json",
        "role": "calibration_diagnostics",
        "local_path": str(files[1].local_path.resolve()),
        "sha256": files[1].sha256,
        "bytes": 2,
    }


def test_client_stages_a_bundle_and_waits_for_its_upload(tmp_path, emitter_with_hub):
    client, hub = emitter_with_hub
    files = minimal_bundle(tmp_path)
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id="uk-run-1", files=files
    )
    receipt = client.stage_dataset_bundle(request, wait_seconds=20)
    assert receipt["status"] == "uploaded", receipt
    (commit,) = hub.commits(PRIVATE_REPOSITORY)
    assert receipt["revision"] == commit.oid
    assert set(commit.added) == {f"staged/uk-run-1/{item.name}" for item in files}
    assert client.dataset_bundle_receipt(request.handover_id()) == receipt
    access = client.check_write_access(PRIVATE_REPOSITORY)
    assert (access["status"], access["can_write"]) == ("checked", True)


def test_client_without_the_module_reports_unavailable(tmp_path, in_thread_emitter):
    client = EmitterClient(
        in_thread_emitter(
            SocketTransport(tmp_path / "unused.sock"),
            {"modules": ["telemetry"], "protocol_versions": [1]},
        )
    )
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id="uk-run-1",
        files=minimal_bundle(tmp_path),
    )
    receipt = client.stage_dataset_bundle(request, wait_seconds=1)
    assert (receipt["status"], receipt["handover_id"]) == (
        "unavailable",
        request.handover_id(),
    )
    assert client.check_write_access(PRIVATE_REPOSITORY)["status"] == "unavailable"

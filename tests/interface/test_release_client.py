"""Typed release publication: release, build, whether current, files with roles."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.identifiers import ROLE_IDENTIFIER, SAFE_IDENTIFIER
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseFile,
    WithdrawBuild,
)
from microcosm_emitter_interface.releases import ReleasesRecord
from microcosm_emitter_interface.transport import SocketTransport

RELEASE = "microcosm-uk-2024-25-national"
BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"


def release_file(directory: Path, name: str, role: str, content: bytes) -> ReleaseFile:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    return ReleaseFile(
        name=name,
        role=role,
        local_path=directory / name,
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def build_files(directory: Path, build: str = BUILD) -> tuple[ReleaseFile, ...]:
    return (
        release_file(
            directory,
            "release_manifest.json",
            "release_manifest",
            json.dumps({"release": RELEASE, "build": build}).encode(),
        ),
        release_file(directory, "microcosm_uk_2024_25.h5", "dataset", b"rows"),
    )


def publication(directory: Path, **changes) -> PublishRelease:
    arguments = {
        "repository": PRIVATE_REPOSITORY,
        "release": RELEASE,
        "build": BUILD,
        "release_role": "national_default",
        "make_current": False,
    }
    arguments.update(changes)
    if "files" not in arguments:
        arguments["files"] = build_files(directory)
    return PublishRelease(**arguments)


MANIFEST_REQUIRED = (
    "A build has one release_manifest.json with the role release_manifest."
)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (lambda files: {"files": files[1:]}, MANIFEST_REQUIRED),
        (
            lambda files: {
                "files": (
                    ReleaseFile(
                        name="release_manifest.json",
                        role="dataset",
                        local_path=files[0].local_path,
                        sha256=files[0].sha256,
                        bytes=files[0].bytes,
                    ),
                    files[1],
                )
            },
            MANIFEST_REQUIRED,
        ),
        (
            lambda files: {
                "files": (
                    *files,
                    ReleaseFile(
                        name="latest-national.json",
                        role="pointer",
                        local_path=files[0].local_path,
                        sha256=files[0].sha256,
                        bytes=files[0].bytes,
                    ),
                )
            },
            "A release file may not be a latest*.json pointer.",
        ),
        (lambda files: {"files": (*files, files[1])}, "A build names each file once."),
        (
            lambda files: {"release": "../escape"},
            f"release must match {SAFE_IDENTIFIER.pattern}.",
        ),
        (
            lambda files: {"build": "two/parts"},
            f"build must match {SAFE_IDENTIFIER.pattern}.",
        ),
        (
            lambda files: {"release_role": "National Default"},
            f"release_role must match {ROLE_IDENTIFIER.pattern}.",
        ),
        (lambda files: {"make_current": "yes"}, "make_current must be a boolean."),
    ],
    ids=[
        "no-manifest",
        "manifest-role",
        "pointer-file",
        "duplicate-file",
        "release",
        "build",
        "role",
        "make-current",
    ],
)
def test_publications_that_break_the_rules_cannot_be_built(tmp_path, changes, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        publication(tmp_path, **changes(build_files(tmp_path)))


def test_publication_message_names_release_build_and_each_file_role(tmp_path):
    request = publication(tmp_path, make_current=True)
    message = request.message()
    assert {key: message[key] for key in ("command", "release", "build")} == {
        "command": "publish_release",
        "release": RELEASE,
        "build": BUILD,
    }
    assert message["make_current"] is True
    assert message["release_role"] == "national_default"
    assert [(entry["name"], entry["role"]) for entry in message["files"]] == [
        ("release_manifest.json", "release_manifest"),
        ("microcosm_uk_2024_25.h5", "dataset"),
    ]
    # The hand-over identity follows the request, not only its content.
    resent = publication(tmp_path, make_current=True, request_id=request.request_id)
    assert request.handover_id() == resent.handover_id()
    assert (
        request.handover_id() != publication(tmp_path, make_current=True).handover_id()
    )
    assert (
        request.handover_id()
        != publication(tmp_path, request_id=request.request_id).handover_id()
    )


def test_client_publishes_promotes_and_withdraws_through_the_emitter(
    tmp_path, emitter_with_hub
):
    client, hub = emitter_with_hub
    first = publication(tmp_path / "first", make_current=True)
    receipt = client.publish_release(first, wait_seconds=20)
    assert receipt["status"] == "published", receipt
    second_build = "uk-frs-calibration-attempt-20261020T000000Z-0000aaaa"
    second = publication(
        tmp_path / "second",
        build=second_build,
        files=build_files(tmp_path / "second", second_build),
    )
    second_receipt = client.publish_release(second, wait_seconds=20)
    assert second_receipt["status"] == "published", second_receipt
    promoted = client.promote_build(
        PromoteBuild(
            repository=PRIVATE_REPOSITORY, release=RELEASE, build=second_build
        ),
        wait_seconds=20,
    )
    assert promoted["status"] == "published", promoted
    withdrawn = client.withdraw_build(
        WithdrawBuild(repository=PRIVATE_REPOSITORY, release=RELEASE, build=BUILD),
        wait_seconds=20,
    )
    assert withdrawn["status"] == "published", withdrawn
    record = ReleasesRecord.parse(hub.files(PRIVATE_REPOSITORY)["releases.json"])
    assert record.releases[RELEASE].current == second_build
    assert record.releases[RELEASE].builds[BUILD].withdrawn is True
    assert client.release_receipt(first.handover_id())["status"] == "published"


def test_client_without_the_module_reports_unavailable(tmp_path, in_thread_emitter):
    client = EmitterClient(
        in_thread_emitter(
            SocketTransport(tmp_path / "unused.sock"),
            {"modules": ["telemetry"], "protocol_versions": [1]},
        )
    )
    receipt = client.publish_release(publication(tmp_path), wait_seconds=1)
    assert (receipt["status"], receipt["release"], receipt["build"]) == (
        "unavailable",
        RELEASE,
        BUILD,
    )


@pytest.mark.parametrize("change", [PromoteBuild, WithdrawBuild])
def test_each_request_has_its_own_request_id_unless_one_is_given(change, tmp_path):
    """A new request object is a new request; resending one is a retry."""
    first = change(repository=PRIVATE_REPOSITORY, release=RELEASE, build=BUILD)
    second = change(repository=PRIVATE_REPOSITORY, release=RELEASE, build=BUILD)
    assert first.request_id != second.request_id
    assert first.handover_id() != second.handover_id()
    assert first.message()["request_id"] == first.request_id
    given = change(
        repository=PRIVATE_REPOSITORY,
        release=RELEASE,
        build=BUILD,
        request_id="operator-request-1",
    )
    assert given.message()["request_id"] == "operator-request-1"
    assert (
        given.handover_id()
        == change(
            repository=PRIVATE_REPOSITORY,
            release=RELEASE,
            build=BUILD,
            request_id="operator-request-1",
        ).handover_id()
    )


def test_publications_carry_a_request_id(tmp_path):
    first = publication(tmp_path / "first")
    second = publication(tmp_path / "first")
    assert first.request_id != second.request_id
    assert first.message()["request_id"] == first.request_id


@pytest.mark.parametrize("request_id", ["", "../escape", "two words", 7])
def test_a_request_id_must_be_a_safe_identifier(request_id):
    with pytest.raises(
        ValueError, match=re.escape(f"request_id must match {SAFE_IDENTIFIER.pattern}.")
    ):
        PromoteBuild(
            repository=PRIVATE_REPOSITORY,
            release=RELEASE,
            build=BUILD,
            request_id=request_id,
        )


def test_a_client_receipt_names_the_build_that_is_current(tmp_path, emitter_with_hub):
    client, _ = emitter_with_hub
    receipt = client.publish_release(
        publication(tmp_path / "first", make_current=True), wait_seconds=20
    )
    assert receipt["status"] == "published", receipt
    assert receipt.get("current") == BUILD, (
        "the receipt does not say which build is current"
    )


def test_an_unavailable_release_receipt_names_no_current_build(
    tmp_path, in_thread_emitter
):
    client = EmitterClient(
        in_thread_emitter(
            SocketTransport(tmp_path / "unused.sock"),
            {"modules": ["telemetry"], "protocol_versions": [1]},
        )
    )
    receipt = client.publish_release(publication(tmp_path), wait_seconds=1)
    assert receipt["status"] == "unavailable"
    assert "current" in receipt, "the receipt does not say which build is current"
    assert receipt["current"] is None

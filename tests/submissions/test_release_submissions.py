"""Release publications reach the collector after their release commit, if switched on.

The module tests send the ``publish_release`` message as the interface sends
it, ``collector`` included, so they run against any emitter.
"""

import hashlib
import json

from fake_hugging_face import PRIVATE_REPOSITORY

from microcosm_emitter_interface.bundles import handover_identifier
from microcosm_emitter_interface.publication import (
    RELEASE_CHANGE,
    PublishRelease,
    ReleaseFile,
)
from microcosm_emitter_interface.submissions import CollectorProvenance

RELEASE = "microcosm-uk-2024-25-national"
BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
PROVENANCE = CollectorProvenance(
    country_code="UK", build_id=BUILD, licensed_data=True, release=RELEASE
)
DIAGNOSTICS = b'{"schema": 8}'
BUILD_MANIFEST = b'{"outputs": {}}'


def release_contents(build=BUILD):
    return {
        "release_manifest.json": (
            "release_manifest",
            json.dumps({"release": RELEASE, "build": build}).encode(),
        ),
        "microcosm_uk_2024_25.h5": ("dataset", b"synthetic h5 bytes"),
        "calibration_diagnostics.json": ("calibration_diagnostics", DIAGNOSTICS),
        "build_manifest.json": ("build_manifest", BUILD_MANIFEST),
    }


def publication(controls, *, collector=PROVENANCE, request_id="release-request-1"):
    """Write the build's files; return the ``publish_release`` message for them."""
    directory = controls.tmp_path / "releases" / BUILD
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name, (role, content) in release_contents().items():
        (directory / name).write_bytes(content)
        files.append(
            ReleaseFile(
                name=name,
                role=role,
                local_path=directory / name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    message = PublishRelease(
        repository=PRIVATE_REPOSITORY,
        release=RELEASE,
        build=BUILD,
        release_role="national_default",
        make_current=True,
        files=files,
        request_id=request_id,
    ).message()
    if collector is not None:
        message["collector"] = collector.message()
    return message


def build_file(controls, name):
    return controls.tmp_path / "releases" / BUILD / name


def handover_id_of(message):
    return handover_identifier(RELEASE_CHANGE, message)


def hand_over(controls, message):
    """Hand the publication over; its receipt names the identifier of all of it.

    The ``collector`` field is part of the queued request, so it is part of
    the identifier.
    """
    receipt = controls.module.query(message)["receipt"]
    assert receipt["handover_id"] == handover_id_of(message)
    return receipt


def release_receipt(controls, message):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "handover_receipt",
            "handover_id": handover_id_of(message),
        }
    )["receipt"]


def collector_receipt(controls, message):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "collector_submission_receipt",
            "handover_id": handover_id_of(message),
        }
    )["receipt"]


def cycle(controls):
    controls.module.delivery.run_cycle()
    if controls.module.collector_delivery is not None:
        controls.module.collector_delivery.run_cycle()


def test_a_described_publication_reaches_the_collector_after_its_release_commit(
    submitting_module,
):
    controls = submitting_module
    message = publication(controls)
    assert hand_over(controls, message)["status"] == "pending"

    cycle(controls)

    release_commit = controls.hub.commits(PRIVATE_REPOSITORY)[-1]
    published = release_receipt(controls, message)
    assert (published["status"], published["commit"]) == (
        "published",
        release_commit.oid,
    )
    (created,) = controls.collector.created
    assert created["kind"] == "calibration-diagnostics"
    assert created["licensed_data"] is True
    assert created["provenance"] == {
        "run_id": BUILD,
        "build_id": BUILD,
        "country_code": "UK",
        "release": RELEASE,
        "huggingface": {
            "repo": PRIVATE_REPOSITORY,
            "commit": release_commit.oid,
            "folder": f"releases/{RELEASE}/{BUILD}",
        },
    }
    assert [(file["name"], file["role"]) for file in created["files"]] == [
        ("build_manifest.json", "build_manifest"),
        ("calibration_diagnostics.json", "calibration_diagnostics"),
        ("release_manifest.json", "release_manifest"),
    ]
    receipt = collector_receipt(controls, message)
    assert receipt["handover_id"] == handover_id_of(message)
    assert receipt["status"] == "awaiting_final_status"
    [submitted] = receipt["submissions"]
    assert submitted["kind"] == "calibration-diagnostics"
    assert submitted["submission_id"] in controls.collector.submissions


def test_the_collector_waits_for_the_release_commit(submitting_module):
    controls = submitting_module
    message = publication(controls)
    hand_over(controls, message)
    controls.remove_credential()

    cycle(controls)
    waiting = collector_receipt(controls, message)
    controls.restore_credential()
    controls.clock.advance(600)
    cycle(controls)

    assert (waiting["status"], waiting["error_code"]) == (
        "pending",
        "WAITING_FOR_HUGGING_FACE",
    )
    assert controls.collector.created
    assert collector_receipt(controls, message)["status"] == "awaiting_final_status"


def test_a_publication_hugging_face_refused_is_not_submitted(submitting_module):
    controls = submitting_module
    message = publication(controls)
    hand_over(controls, message)
    # The dataset is not submitted, so it is not copied: the release writer
    # re-hashes it and refuses the publication.
    dataset = build_file(controls, "microcosm_uk_2024_25.h5")
    dataset.write_bytes(b"X" * len(dataset.read_bytes()))

    cycle(controls)
    controls.clock.advance(600)
    cycle(controls)

    assert release_receipt(controls, message)["status"] == "refused"
    receipt = collector_receipt(controls, message)
    assert (receipt["status"], receipt["error_code"]) == (
        "skipped",
        "NOT_PUBLISHED_ON_HUGGING_FACE",
    )
    assert receipt["submissions"] == []
    assert controls.collector.created == []


def test_a_publication_without_provenance_goes_only_to_hugging_face(
    submitting_module,
):
    controls = submitting_module
    message = publication(controls, collector=None)
    hand_over(controls, message)

    cycle(controls)

    assert release_receipt(controls, message)["status"] == "published"
    assert collector_receipt(controls, message) is None
    assert controls.collector.created == []


def test_publications_are_not_submitted_by_default(default_module):
    controls = default_module
    message = publication(controls)
    hand_over(controls, message)

    cycle(controls)

    assert release_receipt(controls, message)["status"] == "published"
    assert collector_receipt(controls, message) is None
    assert controls.collector.exchange_requests == []


def test_a_publication_submits_its_files_as_they_were_handed_over(
    submitting_module,
):
    controls = submitting_module
    message = publication(controls)
    hand_over(controls, message)
    controls.module.delivery.run_cycle()
    assert release_receipt(controls, message)["status"] == "published"

    # Once the release has landed, the build may rewrite or remove its files.
    build_file(controls, "calibration_diagnostics.json").write_bytes(b'{"schema": 9}')
    build_file(controls, "build_manifest.json").unlink()
    controls.module.collector_delivery.run_cycle()

    assert collector_receipt(controls, message)["status"] == "awaiting_final_status"
    assert sorted(controls.collector.objects.values()) == sorted(
        [
            BUILD_MANIFEST,
            DIAGNOSTICS,
            release_contents()["release_manifest.json"][1],
        ]
    )


def test_an_already_published_build_names_the_commit_that_added_its_folder(
    submitting_module,
):
    controls = submitting_module
    first = publication(controls, collector=None, request_id="release-request-1")
    hand_over(controls, first)
    cycle(controls)
    folder_commit = release_receipt(controls, first)["commit"]
    controls.hub.write_directly(PRIVATE_REPOSITORY, {"notes/later.json": b"{}"})
    later_head = controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid

    # The same build handed over again, now described: the writer finds it
    # published, and the submission names the commit that added its folder.
    again = publication(controls, request_id="release-request-2")
    hand_over(controls, again)
    cycle(controls)

    assert release_receipt(controls, again)["status"] == "published"
    assert folder_commit != later_head
    (created,) = controls.collector.created
    assert created["provenance"]["huggingface"]["commit"] == folder_commit


def test_refused_collector_settings_give_a_described_publication_a_skipped_receipt(
    module_factory, refused_collector_settings
):
    controls = module_factory.start(
        collector_submissions=True, **refused_collector_settings
    )
    try:
        message = publication(controls)
        hand_over(controls, message)
        cycle(controls)
        plain = publication(controls, collector=None, request_id="release-request-2")
        hand_over(controls, plain)

        assert release_receipt(controls, message)["status"] == "published"
        assert collector_receipt(controls, message) == {
            "handover_id": handover_id_of(message),
            "destination": "collector",
            "status": "skipped",
            "error_code": "COLLECTOR_SETTINGS_REFUSED",
            "attempts": 0,
            "submissions": [],
        }
        assert collector_receipt(controls, plain) is None
    finally:
        controls.module.close(0)
    assert controls.collector.exchange_requests == []

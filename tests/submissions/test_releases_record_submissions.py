"""Promotions and withdrawals send the collector the ``releases.json`` they wrote.

A promotion or withdrawal with collector provenance, handed over while
collector submissions are switched on, is submitted after its
``releases.json`` commit lands as one ``releases-record`` submission: that
commit's ``releases.json``, naming the build that is current afterwards. The
module tests send the messages as the interface sends them, ``collector``
included, so they run against any emitter.
"""

import hashlib
import json

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY
from given_token import given_token_variables

from microcosm_emitter_interface.bundles import handover_identifier
from microcosm_emitter_interface.publication import (
    RELEASE_CHANGE,
    PromoteBuild,
    PublishRelease,
    ReleaseFile,
    WithdrawBuild,
)
from microcosm_emitter_interface.submissions import CollectorProvenance

RELEASE = "microcosm-uk-2024-25-national"
CURRENT_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
LATER_BUILD = "uk-frs-calibration-attempt-20261102T101500Z-91ab33c0"
#: When the emitter first checks an accepted submission's status.
FIRST_STATUS_CHECK_SECONDS = 10
#: How long a release change may stay pending before it is refused.
RELEASE_CHANGE_EXPIRY_SECONDS = 3600
#: The two changes to releases.json alone, by their commands.
RECORD_CHANGES = {"promote_build": PromoteBuild, "withdraw_build": WithdrawBuild}


def provenance(build):
    return CollectorProvenance(
        country_code="UK", build_id=build, licensed_data=True, release=RELEASE
    )


def publish(controls, build, *, make_current, request_id):
    """Publish ``build`` without provenance and let it land; return its receipt."""
    directory = controls.tmp_path / "releases" / build
    directory.mkdir(parents=True, exist_ok=True)
    contents = {
        "release_manifest.json": (
            "release_manifest",
            json.dumps({"release": RELEASE, "build": build}).encode(),
        ),
        "microcosm_uk_2024_25.h5": ("dataset", b"synthetic h5 bytes"),
    }
    files = []
    for name, (role, content) in contents.items():
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
        build=build,
        release_role="national_default",
        make_current=make_current,
        files=files,
        request_id=request_id,
    ).message()
    controls.module.query(message)
    controls.module.delivery.run_cycle()
    receipt = release_receipt(controls, message)
    assert receipt["status"] == "published"
    return receipt


def publish_two_builds(controls):
    """Publish ``CURRENT_BUILD`` as the current build and ``LATER_BUILD`` beside it."""
    publish(controls, CURRENT_BUILD, make_current=True, request_id="publish-current")
    publish(controls, LATER_BUILD, make_current=False, request_id="publish-later")


def record_change(command, build, *, collector="own", request_id="change-request-1"):
    """Return the promotion or withdrawal message of ``build``.

    ``collector`` is the provenance to send: by default ``build``'s own, or
    ``None`` for none.
    """
    message = RECORD_CHANGES[command](
        repository=PRIVATE_REPOSITORY,
        release=RELEASE,
        build=build,
        request_id=request_id,
    ).message()
    if collector == "own":
        collector = provenance(build)
    if collector is not None:
        message["collector"] = collector.message()
    return message


def handover_id_of(message):
    return handover_identifier(RELEASE_CHANGE, message)


def hand_over(controls, message):
    """Hand the change over; its receipt names the identifier of all of it.

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


def record_at(controls, commit):
    return controls.hub.files(PRIVATE_REPOSITORY, revision=commit)["releases.json"]


def releases_record_body(*, build, commit, record):
    """Return the ``POST /v1/submissions`` body naming ``build`` and ``commit``."""
    return {
        "contract_version": 1,
        "kind": "releases-record",
        "licensed_data": True,
        "provenance": {
            "run_id": build,
            "build_id": build,
            "country_code": "UK",
            "release": RELEASE,
            "huggingface": {
                "repo": PRIVATE_REPOSITORY,
                "commit": commit,
                "folder": f"releases/{RELEASE}/{build}",
            },
        },
        "files": [
            {
                "name": "releases.json",
                "role": "releases_record",
                "size": len(record),
                "sha256": hashlib.sha256(record).hexdigest(),
            }
        ],
    }


def test_a_described_promotion_submits_the_releases_json_it_committed(
    submitting_module,
):
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change("promote_build", LATER_BUILD)
    assert hand_over(controls, message)["status"] == "pending"

    cycle(controls)

    promotion_commit = controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid
    landed = release_receipt(controls, message)
    assert (landed["status"], landed["commit"], landed["current"]) == (
        "published",
        promotion_commit,
        LATER_BUILD,
    )
    record = record_at(controls, promotion_commit)
    assert json.loads(record)["releases"][RELEASE]["current"] == LATER_BUILD
    assert controls.collector.created == [
        releases_record_body(build=LATER_BUILD, commit=promotion_commit, record=record)
    ]
    assert list(controls.collector.objects.values()) == [record]
    receipt = collector_receipt(controls, message)
    assert receipt["handover_id"] == handover_id_of(message)
    assert receipt["status"] == "awaiting_final_status"
    [submitted] = receipt["submissions"]
    assert submitted["kind"] == "releases-record"
    assert submitted["submission_id"] in controls.collector.submissions


def test_a_described_withdrawal_submits_the_releases_json_naming_the_current_build(
    submitting_module,
):
    """A withdrawal leaves ``current`` as it was; the record names that build."""
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change("withdraw_build", LATER_BUILD)
    hand_over(controls, message)

    cycle(controls)

    withdrawal_commit = controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid
    landed = release_receipt(controls, message)
    assert (landed["status"], landed["commit"], landed["current"]) == (
        "published",
        withdrawal_commit,
        CURRENT_BUILD,
    )
    record = record_at(controls, withdrawal_commit)
    builds = json.loads(record)["releases"][RELEASE]["builds"]
    assert builds[LATER_BUILD]["withdrawn"] is True
    assert controls.collector.created == [
        releases_record_body(
            build=CURRENT_BUILD, commit=withdrawal_commit, record=record
        )
    ]
    assert list(controls.collector.objects.values()) == [record]
    assert collector_receipt(controls, message)["status"] == "awaiting_final_status"


def test_a_withdrawal_that_leaves_no_current_build_sends_nothing(submitting_module):
    controls = submitting_module
    publish(controls, LATER_BUILD, make_current=False, request_id="publish-later")
    message = record_change("withdraw_build", LATER_BUILD)
    hand_over(controls, message)

    cycle(controls)

    landed = release_receipt(controls, message)
    assert (landed["status"], landed["current"]) == ("published", None)
    receipt = collector_receipt(controls, message)
    assert receipt == {
        "handover_id": handover_id_of(message),
        "destination": "collector",
        "status": "skipped",
        "error_code": "NO_CURRENT_BUILD",
        "attempts": 1,
        "submissions": [],
    }
    assert controls.collector.created == []
    assert controls.collector.exchange_requests == []
    assert controls.module.queue.copied_files(handover_id_of(message)) == {}


def test_promoting_the_current_build_again_submits_the_releases_json_at_mains_head(
    submitting_module,
):
    """Nothing is written: the receipt's and the record's commit is ``main``'s head."""
    controls = submitting_module
    publish(controls, CURRENT_BUILD, make_current=True, request_id="publish-current")
    controls.hub.write_directly(PRIVATE_REPOSITORY, {"notes/later.json": b"{}"})
    head = controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid
    message = record_change("promote_build", CURRENT_BUILD)
    hand_over(controls, message)

    cycle(controls)

    landed = release_receipt(controls, message)
    assert controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid == head
    assert (landed["status"], landed["commit"], landed["current"]) == (
        "published",
        head,
        CURRENT_BUILD,
    )
    record = record_at(controls, head)
    assert controls.collector.created == [
        releases_record_body(build=CURRENT_BUILD, commit=head, record=record)
    ]
    assert list(controls.collector.objects.values()) == [record]


def test_a_refused_promotion_is_not_submitted(submitting_module):
    controls = submitting_module
    publish(controls, CURRENT_BUILD, make_current=True, request_id="publish-current")
    message = record_change("promote_build", LATER_BUILD)
    hand_over(controls, message)

    cycle(controls)
    controls.clock.advance(600)
    cycle(controls)

    assert release_receipt(controls, message)["status"] == "refused"
    receipt = collector_receipt(controls, message)
    assert (receipt["status"], receipt["error_code"], receipt["submissions"]) == (
        "skipped",
        "NOT_PUBLISHED_ON_HUGGING_FACE",
        [],
    )
    assert controls.collector.created == []


@pytest.mark.parametrize("command", sorted(RECORD_CHANGES))
def test_a_promotion_or_withdrawal_without_provenance_goes_only_to_hugging_face(
    submitting_module, command
):
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change(command, LATER_BUILD, collector=None)
    hand_over(controls, message)

    cycle(controls)

    assert release_receipt(controls, message)["status"] == "published"
    assert collector_receipt(controls, message) is None
    assert controls.collector.created == []


@pytest.mark.parametrize("command", sorted(RECORD_CHANGES))
def test_promotions_and_withdrawals_are_not_submitted_by_default(
    default_module, command
):
    controls = default_module
    publish_two_builds(controls)
    message = record_change(command, LATER_BUILD)
    hand_over(controls, message)

    cycle(controls)

    assert release_receipt(controls, message)["status"] == "published"
    assert collector_receipt(controls, message) is None
    assert controls.collector.exchange_requests == []
    assert controls.module.queue.copied_files(handover_id_of(message)) == {}


@pytest.mark.parametrize("command", sorted(RECORD_CHANGES))
def test_a_change_whose_provenance_names_another_build_is_refused(
    submitting_module, command
):
    controls = submitting_module
    message = record_change(command, LATER_BUILD, collector=provenance(CURRENT_BUILD))

    receipt = controls.module.query(message)["receipt"]

    assert (receipt["status"], receipt["error_code"]) == ("refused", "INVALID_REQUEST")
    assert collector_receipt(controls, message) is None


def test_the_collector_waits_for_the_releases_json_commit(submitting_module):
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change("promote_build", LATER_BUILD)
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
    [created] = controls.collector.created
    assert created["kind"] == "releases-record"
    assert collector_receipt(controls, message)["status"] == "awaiting_final_status"


def test_the_kept_releases_json_is_removed_once_both_rows_have_settled(
    submitting_module,
):
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change("promote_build", LATER_BUILD)
    hand_over(controls, message)
    controls.module.delivery.run_cycle()
    promotion_commit = release_receipt(controls, message)["commit"]
    queue = controls.module.queue
    kept = queue.copied_files(handover_id_of(message))
    controls.module.collector_delivery.run_cycle()
    (submission_id,) = controls.collector.submissions
    controls.collector.finish(submission_id, "succeeded")

    controls.clock.advance(FIRST_STATUS_CHECK_SECONDS)
    controls.module.collector_delivery.run_cycle()

    assert kept == {"releases.json": record_at(controls, promotion_commit)}
    assert collector_receipt(controls, message)["status"] == "succeeded"
    assert queue.copied_files(handover_id_of(message)) == {}


def test_a_promotion_that_lands_before_its_hand_over_is_answered_is_still_submitted(
    submitting_module, monkeypatch
):
    """The release worker may finish the change before the query returns."""
    controls = submitting_module
    publish_two_builds(controls)
    queue = controls.module.queue
    queue_job = queue.enqueue_job

    def queue_then_land(**fields):
        receipt = queue_job(**fields)
        if fields["kind"] == RELEASE_CHANGE:
            controls.module.delivery.run_cycle()
        return receipt

    monkeypatch.setattr(queue, "enqueue_job", queue_then_land)
    message = record_change("promote_build", LATER_BUILD)
    hand_over(controls, message)
    monkeypatch.undo()

    controls.module.collector_delivery.run_cycle()

    promotion_commit = release_receipt(controls, message)["commit"]
    assert controls.collector.created == [
        releases_record_body(
            build=LATER_BUILD,
            commit=promotion_commit,
            record=record_at(controls, promotion_commit),
        )
    ]


def test_a_collector_row_waits_until_its_hugging_face_row_is_queued(
    submitting_module,
):
    """It is queued first, so it waits, at most until the change would expire."""
    controls = submitting_module
    message = record_change("promote_build", LATER_BUILD)
    delivery = controls.module.collector_delivery
    delivery.enqueue_for_handover(handover_id_of(message), message)

    delivery.run_cycle()
    waiting = collector_receipt(controls, message)
    controls.clock.advance(RELEASE_CHANGE_EXPIRY_SECONDS + 1)
    delivery.run_cycle()

    assert (waiting["status"], waiting["error_code"]) == (
        "pending",
        "WAITING_FOR_HUGGING_FACE",
    )
    receipt = collector_receipt(controls, message)
    assert (receipt["status"], receipt["error_code"]) == (
        "skipped",
        "NOT_PUBLISHED_ON_HUGGING_FACE",
    )
    assert controls.collector.created == []


def test_a_change_that_landed_without_keeping_its_releases_json_is_skipped(
    submitting_module,
):
    """An emitter release that does not keep it may land the change."""
    controls = submitting_module
    publish_two_builds(controls)
    message = record_change("promote_build", LATER_BUILD)
    hand_over(controls, message)
    queue = controls.module.queue
    job = queue.claim_job(PRIVATE_REPOSITORY, now=controls.clock(), lease_seconds=120)
    assert job is not None
    head = controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid
    queue.finish_job(
        job,
        receipt={
            **release_receipt(controls, message),
            "status": "published",
            "commit": head,
            "current": LATER_BUILD,
            "attempts": 1,
        },
        retry=False,
        now=controls.clock(),
    )

    controls.module.collector_delivery.run_cycle()

    receipt = collector_receipt(controls, message)
    assert (receipt["status"], receipt["error_code"], receipt["submissions"]) == (
        "skipped",
        "RELEASES_RECORD_MISSING",
        [],
    )
    assert controls.collector.created == []


def test_refused_collector_settings_give_a_described_promotion_a_skipped_receipt(
    module_factory, refused_collector_settings
):
    controls = module_factory.start(
        collector_submissions=True, **refused_collector_settings
    )
    try:
        publish_two_builds(controls)
        message = record_change("promote_build", LATER_BUILD)
        hand_over(controls, message)
        cycle(controls)
        plain = record_change(
            "withdraw_build", LATER_BUILD, collector=None, request_id="change-2"
        )

        assert release_receipt(controls, message)["status"] == "published"
        assert collector_receipt(controls, message) == {
            "handover_id": handover_id_of(message),
            "destination": "collector",
            "status": "skipped",
            "error_code": "COLLECTOR_SETTINGS_REFUSED",
            "attempts": 0,
            "submissions": [],
        }
        hand_over(controls, plain)
        assert collector_receipt(controls, plain) is None
    finally:
        controls.module.close(0)
    assert controls.collector.exchange_requests == []


def test_unusable_given_token_variables_give_a_described_withdrawal_a_skipped_receipt(
    module_factory, collector, monkeypatch
):
    # A token the runner obtained from another collector than this module's.
    for name, value in given_token_variables("https://collector.example.test").items():
        monkeypatch.setenv(name, value)
    controls = module_factory.start(
        collector_submissions=True, development_collector_url=collector.origin
    )
    try:
        message = record_change("withdraw_build", LATER_BUILD)
        hand_over(controls, message)

        receipt = collector_receipt(controls, message)
    finally:
        controls.module.close(0)
    assert (receipt["status"], receipt["error_code"]) == (
        "skipped",
        "COLLECTOR_SETTINGS_REFUSED",
    )

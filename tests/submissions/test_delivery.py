"""Bundles reach the collector after their Hugging Face commit, when switched on.

Each submission's status is then checked until it is final.
"""

import hashlib
import threading
import time

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY, STAGING_REPOSITORY

from microcosm_emitter.core.constants import (
    CLOUD_RUN_COLLECTOR_URL,
    PRODUCTION_COLLECTOR_URL,
    TRUSTED_COLLECTOR_ORIGINS,
)
from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.submissions import CollectorProvenance

PROVENANCE = CollectorProvenance(
    country_code="UK", build_id="uk-frs-calibration-attempt-1", licensed_data=True
)
CONTENTS = {
    "microcosm_uk_2024_25.h5": ("dataset", b"synthetic h5 bytes"),
    "calibration_diagnostics.json": ("calibration_diagnostics", b'{"schema": 8}'),
    "build_manifest.json": ("build_manifest", b'{"outputs": {}}'),
    "staged_manifest.json": ("staged_manifest", b'{"files": {}}'),
    "sha256sums.txt": ("checksums", b"digests\n"),
}
ORRERY_CONTENTS = {
    "graph.schema.json": ("orrery_schema", b'{"schema": true}'),
    "graph.execution.json": ("execution_overlay", b'{"protocol": "overlay"}'),
}


def bundle(
    controls,
    *,
    collector=PROVENANCE,
    contents=CONTENTS,
    run_id="uk-run-1",
    prefix="staged",
):
    directory = controls.tmp_path / run_id
    directory.mkdir(exist_ok=True)
    files = []
    for name, (role, content) in contents.items():
        (directory / name).write_bytes(content)
        files.append(
            BundleFile(
                name=name,
                role=role,
                local_path=directory / name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    return StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id=run_id,
        files=files,
        collector=collector,
        prefix=prefix,
    )


def stage(controls, request):
    return controls.module.query(request.message())["receipt"]


def cycle(controls):
    controls.module.delivery.run_cycle()
    if controls.module.collector_delivery is not None:
        controls.module.collector_delivery.run_cycle()


def collector_receipt(controls, request):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "collector_submission_receipt",
            "handover_id": request.handover_id(),
        }
    )["receipt"]


def test_a_described_bundle_reaches_the_collector_after_its_commit(
    submitting_module,
):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)

    cycle(controls)

    (commit,) = controls.hub.commits(PRIVATE_REPOSITORY)
    (created,) = controls.collector.created
    assert created["kind"] == "calibration-diagnostics"
    assert created["provenance"]["huggingface"] == {
        "repo": PRIVATE_REPOSITORY,
        "commit": commit.oid,
        "folder": "staged/uk-run-1",
    }
    assert [file["name"] for file in created["files"]] == [
        "build_manifest.json",
        "calibration_diagnostics.json",
    ]
    assert sorted(controls.collector.objects.values()) == [
        b'{"outputs": {}}',
        b'{"schema": 8}',
    ]
    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "awaiting_final_status"
    assert receipt["error_code"] is None
    [submitted] = receipt["submissions"]
    assert submitted["kind"] == "calibration-diagnostics"
    assert submitted["status"] == "awaiting_final_status"
    assert submitted["collector_status"] == "queued"
    assert submitted["receipt"] is None
    assert submitted["submission_id"] in controls.collector.submissions


def test_a_bundle_naming_orrery_inputs_submits_both_kinds(submitting_module):
    controls = submitting_module
    request = bundle(controls, contents={**CONTENTS, **ORRERY_CONTENTS})
    stage(controls, request)

    cycle(controls)

    assert sorted(created["kind"] for created in controls.collector.created) == [
        "calibration-diagnostics",
        "orrery-inputs",
    ]
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


def test_collector_submissions_are_off_by_default(default_module):
    controls = default_module
    request = bundle(controls)
    stage(controls, request)

    cycle(controls)

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert controls.module.collector_delivery is None
    assert collector_receipt(controls, request) is None
    assert controls.collector.exchange_requests == []


def test_a_bundle_without_provenance_goes_only_to_hugging_face(submitting_module):
    controls = submitting_module
    request = bundle(controls, collector=None)
    stage(controls, request)

    cycle(controls)

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert collector_receipt(controls, request) is None
    assert controls.collector.created == []


def test_the_collector_waits_for_the_hugging_face_commit(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.remove_credential()

    cycle(controls)
    waiting = collector_receipt(controls, request)
    controls.restore_credential()
    controls.clock.advance(600)
    cycle(controls)

    assert waiting["status"] == "pending"
    assert waiting["error_code"] == "WAITING_FOR_HUGGING_FACE"
    assert controls.collector.created
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


def test_a_bundle_hugging_face_refused_is_not_submitted(submitting_module, monkeypatch):
    from microcosm_emitter.huggingface.service import requests as request_module

    # Too large to copy: the changed file is uploaded from its path and refused.
    monkeypatch.setattr(request_module, "MAX_COPIED_FILE_BYTES", 8)
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    (controls.tmp_path / "uk-run-1" / "calibration_diagnostics.json").write_bytes(
        b'{"schema": 9}'
    )

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "skipped"
    assert receipt["error_code"] == "NOT_STAGED_ON_HUGGING_FACE"
    assert controls.collector.created == []


@pytest.mark.parametrize("intake", ["off", "absent"])
def test_without_intake_the_submission_is_retried_about_hourly(
    submitting_module, intake
):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.collector.intake = intake

    cycle(controls)
    pending = collector_receipt(controls, request)
    controls.collector.intake = "on"
    controls.clock.advance(600)
    cycle(controls)
    still_pending = collector_receipt(controls, request)
    controls.clock.advance(3000)
    cycle(controls)

    assert pending["status"] == "pending"
    assert pending["error_code"] == "COLLECTOR_INTAKE_NOT_ENABLED"
    # Not attempted again within the hour, and nothing was submitted.
    assert still_pending["attempts"] == pending["attempts"]
    assert len(controls.collector.created) == 1
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


def test_a_refused_submission_fails_its_collector_hand_over(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.collector.create_statuses = [422]

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "COLLECTOR_REFUSED_422"


def test_an_invalid_collector_description_refuses_the_bundle(submitting_module):
    controls = submitting_module
    message = bundle(controls).message()
    message["collector"] = {**message["collector"], "country_code": "uk"}

    receipt = controls.module.query(message)["receipt"]

    assert receipt["status"] == "refused"
    assert receipt["error_code"] == "INVALID_REQUEST"


def test_the_module_tick_delivers_to_both_destinations(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)

    controls.module.tick(time.monotonic())

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


def test_files_microcosm_rewrites_after_staging_are_submitted_as_handed_over(
    submitting_module,
):
    """Microcosm rewrites its build manifest right after ``stage_dataset``."""
    controls = submitting_module
    request = bundle(controls)
    receipt = stage(controls, request)
    assert "build_manifest.json" in receipt["copied_files"]
    manifest = controls.tmp_path / "uk-run-1" / "build_manifest.json"
    rewritten = manifest.with_name(".build_manifest.json.rewrite")
    rewritten.write_bytes(b'{"outputs": {}, "staging_delivery": {}}')
    rewritten.replace(manifest)

    cycle(controls)

    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"
    assert sorted(controls.collector.objects.values()) == [
        b'{"outputs": {}}',
        b'{"schema": 8}',
    ]


def test_a_folder_longer_than_the_collector_accepts_is_not_submitted(
    submitting_module,
):
    """The contract allows a folder of at most 512 characters.

    A longer one would be refused with a final 422.
    """
    controls = submitting_module
    long_prefix = "/".join(["p" * 128] * 3)
    request = bundle(controls, run_id="r" * 128, prefix=long_prefix)
    assert len(request.remote_prefix) > 512
    stage(controls, request)

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "skipped",
        "COLLECTOR_FOLDER_TOO_LONG",
    )
    assert controls.collector.created == []
    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1


# A slow or stalled collector submission never holds up Hugging Face ------


def wait_until(condition, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def staging_holds(controls, path_in_repo: str) -> bool:
    return any(
        path_in_repo in commit.added
        for commit in controls.hub.commits(STAGING_REPOSITORY)
    )


def stage_progress(controls, *, flush: bool) -> None:
    from microcosm_emitter_interface.staging import RunFile, StageRunFiles

    progress = controls.tmp_path / "progress.json"
    progress.write_bytes(b'{"stage":"completed"}')
    request = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id="staging-run-1",
        files=(RunFile("progress.json", progress),),
        flush=flush,
        commit_interval_seconds=3600,
    )
    assert controls.module.query(request.message())["receipt"]["status"] == ("recorded")


def described_bundle_with_a_held_upload(controls) -> StageDatasetBundle:
    """Hand over a bundle naming Orrery inputs while the collector holds uploads.

    Returns the bundle once its first upload to the collector has started and is
    held.
    """
    controls.hub.create_repository(STAGING_REPOSITORY, {"README.md": b"staging"})
    controls.collector.hold_uploads()
    request = bundle(controls, contents={**CONTENTS, **ORRERY_CONTENTS})
    stage(controls, request)
    assert controls.collector.upload_started.wait(10), "no upload started"
    return request


@pytest.fixture
def module_with_workers(module_factory, collector):
    """Start the module with its own delivery workers, as the host runs it."""
    controls = module_factory.start(
        background_workers=True,
        # As in the host: a submission waiting for its Hugging Face commit
        # is retried seconds later.
        clock=time.time,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    host_stopped = threading.Event()

    def tick_as_the_host_does():
        while not host_stopped.wait(0.01):
            controls.module.tick(time.monotonic())

    host = threading.Thread(target=tick_as_the_host_does, daemon=True)
    host.start()
    try:
        yield controls
    finally:
        collector.release_uploads()
        host_stopped.set()
        host.join(timeout=10)
        controls.module.close(time.monotonic() + 5)


def test_hugging_face_commits_land_during_a_held_collector_upload(
    module_with_workers,
):
    from microcosm_emitter_interface.publication import PublishRelease, ReleaseFile

    controls = module_with_workers
    described_bundle_with_a_held_upload(controls)
    stage_progress(controls, flush=True)
    manifest = controls.tmp_path / "release_manifest.json"
    manifest.write_bytes(b'{"release": "fixture-release", "build": "fixture-build"}')
    publication = PublishRelease(
        repository=PRIVATE_REPOSITORY,
        release="fixture-release",
        build="fixture-build",
        release_role="national_default",
        make_current=False,
        files=(
            ReleaseFile(
                name="release_manifest.json",
                role="release_manifest",
                local_path=manifest,
                sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                bytes=manifest.stat().st_size,
            ),
        ),
    )
    controls.module.query(publication.message())

    assert wait_until(
        lambda: staging_holds(controls, "runs/staging-run-1/progress.json"),
        seconds=5,
    )
    assert wait_until(
        lambda: (
            controls.module.query(
                {
                    "protocol_version": 1,
                    "command": "handover_receipt",
                    "handover_id": publication.handover_id(),
                }
            )["receipt"]["status"]
            == "published"
        ),
        seconds=5,
    )
    assert not controls.collector.uploads_released.is_set()


def test_the_shutdown_commit_runs_during_a_held_collector_upload(
    module_factory, collector
):
    import shutil
    import tempfile
    from pathlib import Path

    from microcosm_emitter.host.runtime import ServiceRuntime

    controls = module_factory.new(
        background_workers=True,
        clock=time.time,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    runtime_directory = Path(tempfile.mkdtemp(prefix="pe-collector-", dir="/tmp"))
    runtime = ServiceRuntime(
        runtime_directory / "service.sock",
        modules={"huggingface": controls.module},
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
        drain_seconds=2,
    )
    runtime_errors = []

    def run_runtime():
        try:
            runtime.run()
        # Keeps any exception this thread's call raises, for the test to check.
        except BaseException as error:  # noqa: BLE001
            runtime_errors.append(error)

    runtime_thread = threading.Thread(target=run_runtime, daemon=True)
    runtime_thread.start()
    try:
        assert wait_until(lambda: controls.module.queue is not None, seconds=5)
        described_bundle_with_a_held_upload(controls)
        # Not due for an hour: only the commit at shutdown can deliver it.
        stage_progress(controls, flush=False)

        runtime.stop()
        runtime_thread.join(timeout=5)

        assert not runtime_thread.is_alive()
        assert runtime_errors == []
        assert staging_holds(controls, "runs/staging-run-1/progress.json")
    finally:
        collector.release_uploads()
        runtime.stop()
        runtime_thread.join(timeout=10)
        shutil.rmtree(runtime_directory, ignore_errors=True)


def test_a_held_collector_upload_leaves_the_second_shutdown_commit_its_time(
    module_factory, collector
):
    """The second shutdown commit sends the run files while a collector upload is held.

    The #27 fix review's case. At shutdown a bundle upload holds the staging
    repository, so the first shutdown commit skips its run files; that upload
    finishes in time, while a collector upload is still held past the deadline.
    The second shutdown commit must still send them.
    """
    from dataclasses import replace

    controls = module_factory.start(
        background_workers=True,
        clock=time.time,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    try:
        described_bundle_with_a_held_upload(controls)
        bundle_upload_entered = threading.Event()

        def hold_the_bundle_upload_briefly():
            bundle_upload_entered.set()
            time.sleep(0.5)

        controls.hub.run_before_next_commit(hold_the_bundle_upload_briefly)
        undescribed = replace(
            bundle(controls, collector=None, run_id="uk-run-2"),
            repository=STAGING_REPOSITORY,
        )
        assert stage(controls, undescribed)["status"] == "pending"
        assert bundle_upload_entered.wait(10), "the bundle upload never started"
        # No host tick runs here: only a shutdown commit can deliver it.
        stage_progress(controls, flush=False)

        controls.module.close(time.monotonic() + 3)

        assert staging_holds(controls, "runs/staging-run-1/progress.json"), (
            "the staging run files missed the second shutdown commit"
        )
    finally:
        collector.release_uploads()
        controls.module.close(0)


def test_shutdown_waits_for_a_held_collector_upload_only_briefly(
    module_factory, collector, monkeypatch
):
    from microcosm_emitter.huggingface.service import module as module_module

    monkeypatch.setattr(module_module, "COLLECTOR_SHUTDOWN_WAIT_SECONDS", 0.2)
    controls = module_factory.start(
        background_workers=True,
        clock=time.time,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    try:
        described_bundle_with_a_held_upload(controls)
        started = time.monotonic()

        controls.module.close(started + 10)

        assert time.monotonic() - started < 5
    finally:
        collector.release_uploads()
        controls.module.close(0)


def test_a_collector_cycle_claims_nothing_once_shutdown_begins(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.module.delivery.run_cycle()

    attempted = controls.module.collector_delivery.run_cycle(
        stop_requested=lambda: True
    )

    assert attempted == 0
    assert collector_receipt(controls, request)["attempts"] == 0
    assert controls.collector.created == []


def test_uploads_have_a_finite_write_timeout():
    from microcosm_emitter.submissions.client import UPLOAD_TIMEOUT

    assert UPLOAD_TIMEOUT.write is not None and UPLOAD_TIMEOUT.write > 0


def test_a_collector_cycle_starts_no_submission_after_its_time_budget(
    submitting_module, monkeypatch
):
    from microcosm_emitter.submissions import delivery as delivery_module

    controls = submitting_module
    monkeypatch.setattr(delivery_module, "MAX_CYCLE_SECONDS", 0.0)
    for run_id in ("uk-run-1", "uk-run-2"):
        stage(controls, bundle(controls, run_id=run_id))
    controls.module.delivery.run_cycle()
    controls.clock.advance(20)
    controls.module.delivery.run_cycle()

    assert controls.module.collector_delivery.run_cycle() == 1
    assert controls.module.collector_delivery.run_cycle() == 1


# Collector problems stay out of the Hugging Face path ---------------------


def test_a_collector_queue_error_leaves_the_bundle_answer_alone(
    submitting_module, capsys
):
    controls = submitting_module

    def locked_database(*arguments, **options):
        error_message = "database is locked"
        raise RuntimeError(error_message)

    controls.module.collector_delivery.enqueue_for_bundle = locked_database
    request = bundle(controls)

    receipt = stage(controls, request)

    assert (receipt["status"], receipt["handover_id"]) == (
        "pending",
        request.handover_id(),
    )
    warning = capsys.readouterr().err
    assert "collector" in warning and "database is locked" not in warning


def test_an_unusable_collector_setting_switches_submissions_off(module_factory, capsys):
    controls = module_factory.start(
        collector_submissions=True,
        development_collector_url="https://collector.example.test",
    )
    try:
        assert controls.module.collector_delivery is None
        assert stage(controls, bundle(controls))["status"] == "pending"
        controls.module.delivery.run_cycle()
        assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    finally:
        controls.module.close(0)
    assert "collector submissions are off" in capsys.readouterr().err


def test_an_unexpected_error_in_a_submission_releases_its_row(
    submitting_module,
):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.module.delivery.run_cycle()

    def unexpected(planned):
        error_message = "synthetic"
        raise RuntimeError(error_message)

    controls.module.collector_delivery.client.submit = unexpected
    controls.module.collector_delivery.run_cycle()

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == ("pending", "LOCAL_ERROR")
    # Retried after the usual backoff, not after the hour-long lease.
    controls.clock.advance(5)
    del controls.module.collector_delivery.client.submit
    assert controls.module.collector_delivery.run_cycle() == 1
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


# The collector row's lease while an attempt runs ---------------------------
#
# Every emitter of a user on one machine shares the hand-over database by
# default, so a second emitter can claim the collector row once its lease
# has passed. While the first emitter's attempt is uploading, its lease must
# be kept, however long the upload takes; once that emitter stops, the row
# must be free again within one lease.

#: Shortened leases, so a lease that is not renewed passes within the test.
SHORT_SUBMISSION_LEASE_SECONDS = 1.0
SHORT_LEASE_RENEWAL_SECONDS = 0.2
#: Two and a half leases that are not renewed.
OBSERVED_SECONDS = 2.5


class RecordingSubmissionClient:
    """A second emitter's collector client, which sends nothing.

    It records each submission it is asked to make.
    """

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.submitted: list[str] = []

    def submit(self, planned):
        from microcosm_emitter.submissions.client import SubmissionOutcome

        self.submitted.append(planned.kind)
        return SubmissionOutcome("pending", "UPLOAD_FAILED")

    def close(self) -> None:
        """Nothing to close: the module calls this when it stops."""


@pytest.fixture
def short_collector_leases(monkeypatch):
    from microcosm_emitter.huggingface.service import module as module_module
    from microcosm_emitter.submissions import delivery as delivery_module

    monkeypatch.setattr(
        delivery_module, "SUBMISSION_LEASE_SECONDS", SHORT_SUBMISSION_LEASE_SECONDS
    )
    monkeypatch.setattr(
        delivery_module,
        "SUBMISSION_LEASE_RENEWAL_SECONDS",
        SHORT_LEASE_RENEWAL_SECONDS,
        raising=False,
    )
    monkeypatch.setattr(module_module, "COLLECTOR_SHUTDOWN_WAIT_SECONDS", 0.2)


@pytest.fixture
def two_emitters(module_factory, collector, short_collector_leases):
    """Yield two emitters on one hand-over database, and the second one's client.

    The first emitter runs its own workers; the second is a collector delivery on
    the same hand-over database, driven by the test.
    """
    from microcosm_emitter.huggingface.service.queue import HandoverQueue
    from microcosm_emitter.submissions.delivery import CollectorSubmissionDelivery

    first = module_factory.start(
        background_workers=True,
        clock=time.time,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    second_queue = HandoverQueue(first.tmp_path / "events.handovers.sqlite3")
    second_client = RecordingSubmissionClient(first.module.collector_delivery.target)
    second = CollectorSubmissionDelivery(second_queue, second_client, clock=time.time)
    try:
        yield first, second, second_client
    finally:
        collector.release_uploads()
        first.module.close(time.monotonic() + 5)
        second_queue.close()


def test_a_second_emitter_never_claims_a_collector_row_during_its_upload(
    two_emitters,
):
    """A second emitter never claims a collector row during the first one's upload.

    The #27 round-2 review's NEW BUGS 2: the first emitter's upload is held for
    longer than two leases.
    """
    first, second, second_client = two_emitters
    request = described_bundle_with_a_held_upload(first)

    claimed = []
    observed_until = time.monotonic() + OBSERVED_SECONDS
    while time.monotonic() < observed_until:
        claimed.append(second.run_cycle())
        time.sleep(0.1)

    assert not any(claimed) and second_client.submitted == [], (
        "a second emitter claimed the collector row while its upload was running"
    )
    first.collector.release_uploads()
    # Only the first emitter's client can be accepted; the second's records
    # every submission as pending.
    assert wait_until(
        lambda: collector_receipt(first, request)["status"] == "awaiting_final_status",
        seconds=10,
    ), "the first emitter's result was not recorded"


def test_a_collector_row_is_free_within_one_lease_after_shutdown(two_emitters):
    """Shutdown abandons the held upload, and its lease must no longer be kept.

    Another emitter can then take the row over.
    """
    first, second, second_client = two_emitters
    described_bundle_with_a_held_upload(first)

    first.module.close(time.monotonic() + 1)

    assert wait_until(
        lambda: second.run_cycle() > 0,
        seconds=SHORT_SUBMISSION_LEASE_SECONDS * 3,
    ), "the collector row stayed leased after shutdown"
    assert second_client.submitted


# Rows queued under another origin ------------------------------------------
#
# Emitters released before the collector's hostname became the default queue
# collector rows under the Cloud Run address. That address reaches the same
# collector, so an emitter at the default must still deliver those rows after
# the machine upgrades, and a bundle handed over again must join its row.
# Any other origin is another collector, even when it is trusted: its rows wait
# for an emitter at that origin.

#: An origin a later release might trust, such as a staging collector's.
ANOTHER_TRUSTED_ORIGIN = "https://another-collector.example"


@pytest.mark.parametrize(
    ("queued_origin", "delivered_by_default"),
    [(CLOUD_RUN_COLLECTOR_URL, True), (ANOTHER_TRUSTED_ORIGIN, False)],
    ids=["cloud run address", "another trusted origin"],
)
def test_a_default_emitter_takes_only_rows_queued_for_its_own_collector(
    module_factory, collector, capsys, monkeypatch, queued_origin, delivered_by_default
):
    from microcosm_emitter.core import collector_origin as collector_origin_module

    monkeypatch.setattr(
        collector_origin_module,
        "TRUSTED_COLLECTOR_ORIGINS",
        TRUSTED_COLLECTOR_ORIGINS | {ANOTHER_TRUSTED_ORIGIN},
    )

    # As an emitter at that origin: it queues the collector row and commits
    # the bundle to Hugging Face, but is never let reach a collector.
    earlier = module_factory.start(
        collector_submissions=True, collector_origin=queued_origin
    )
    request = bundle(earlier)
    try:
        stage(earlier, request)
        earlier.module.delivery.run_cycle()
    finally:
        earlier.module.close(0)

    # A development collector is not the same collector: it takes no such row.
    development = module_factory.start(
        collector_submissions=True, development_collector_url=collector.origin
    )
    try:
        assert development.module.collector_delivery.run_cycle() == 0
    finally:
        development.module.close(0)

    upgraded = module_factory.start(collector_submissions=True)
    upgraded.module.collector_delivery.client.close()
    recording_client = RecordingSubmissionClient(PRODUCTION_COLLECTOR_URL)
    upgraded.module.collector_delivery.client = recording_client
    try:
        stage(upgraded, request)
        upgraded.module.collector_delivery.run_cycle()
    finally:
        upgraded.module.close(0)

    expected_submissions = ["calibration-diagnostics"] if delivered_by_default else []
    assert recording_client.submitted == expected_submissions
    # Handing the bundle over again joins a row queued for the same collector,
    # and reports a row queued for another one.
    queue_conflict_reported = (
        "could not be queued for the collector" in capsys.readouterr().err
    )
    assert queue_conflict_reported is not delivered_by_default


# Status checks after completion --------------------------------------------
#
# The contract: once completion is accepted, poll
# ``GET /v1/submissions/{id}`` until the status is final, then read
# ``GET /v1/submissions/{id}/receipt``. The emitter does it from the
# hand-over queue: the first check 10 seconds after acceptance, then each
# wait twice the last, up to ten minutes.

FIRST_CHECK_SECONDS = 10


def accepted_bundle(controls, **options):
    """Stage a described bundle and run one delivery cycle.

    The collector accepts the submission's completion in that cycle. Returns the
    request and its submission ID.
    """
    request = bundle(controls, **options)
    stage(controls, request)
    cycle(controls)
    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "awaiting_final_status", receipt
    [entry] = receipt["submissions"]
    return request, entry["submission_id"]


def check_after(controls, seconds):
    controls.clock.advance(seconds)
    controls.module.collector_delivery.run_cycle()


def test_a_succeeded_submission_stores_its_receipt(submitting_module):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    result = {"processed_by": "intake-only", "verified_files": 2}
    controls.collector.finish(submission_id, "succeeded", result=result)

    check_after(controls, FIRST_CHECK_SECONDS)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == ("succeeded", None)
    [entry] = receipt["submissions"]
    assert (entry["status"], entry["collector_status"]) == ("succeeded", "succeeded")
    assert entry["rejection"] is None
    assert entry["receipt"] == controls.collector.receipt_document(submission_id)
    assert entry["receipt"]["result"] == result
    # Settled: never checked again.
    requests = len(controls.collector.status_requests)
    check_after(controls, 3600)
    assert len(controls.collector.status_requests) == requests


def test_a_submission_awaits_its_final_status_while_the_worker_runs(
    submitting_module,
):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)

    check_after(controls, FIRST_CHECK_SECONDS)
    controls.collector.finish(submission_id, "processing")
    check_after(controls, 2 * FIRST_CHECK_SECONDS)

    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "awaiting_final_status"
    [entry] = receipt["submissions"]
    assert (entry["status"], entry["collector_status"]) == (
        "awaiting_final_status",
        "processing",
    )
    assert controls.collector.status_requests == [submission_id, submission_id]
    assert controls.collector.receipt_requests == []


def test_status_checks_back_off_from_ten_seconds_to_ten_minutes(submitting_module):
    controls = submitting_module
    accepted_bundle(controls)
    accepted_at = controls.clock.now
    checked_at = []

    for _ in range(1900):
        check_after(controls, 1)
        while len(checked_at) < len(controls.collector.status_requests):
            checked_at.append(controls.clock.now - accepted_at)

    # Waits of 10, 20, 40, 80, 160 and 320 seconds, then ten minutes.
    assert checked_at == [10, 30, 70, 150, 310, 630, 1230, 1830]


@pytest.mark.parametrize(
    ("final_status", "code"),
    [("rejected", "content_mismatch"), ("failed", "worker_error")],
)
def test_a_submission_that_did_not_succeed_keeps_the_collectors_reason(
    submitting_module, final_status, code
):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    rejection = {"code": code, "message": f"The worker reported {code}."}
    controls.collector.finish(submission_id, final_status, rejection=rejection)

    check_after(controls, FIRST_CHECK_SECONDS)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (final_status, code)
    [entry] = receipt["submissions"]
    assert (entry["status"], entry["error_code"]) == (final_status, code)
    assert entry["rejection"] == rejection
    assert entry["receipt"] == controls.collector.receipt_document(submission_id)


def test_a_rejection_at_completion_stores_its_receipt_in_the_same_attempt(
    submitting_module,
):
    controls = submitting_module
    controls.collector.corrupt_uploads = True
    request = bundle(controls)
    stage(controls, request)

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "rejected",
        "stored_object_mismatch",
    )
    [entry] = receipt["submissions"]
    assert entry["rejection"]["code"] == "stored_object_mismatch"
    # Rejected at completion: no job ran.
    assert entry["receipt"]["job"] is None


def test_one_rejected_kind_makes_the_hand_over_rejected(submitting_module):
    controls = submitting_module
    request = bundle(controls, contents={**CONTENTS, **ORRERY_CONTENTS})
    stage(controls, request)
    cycle(controls)
    entries = {
        entry["kind"]: entry
        for entry in collector_receipt(controls, request)["submissions"]
    }
    rejection = {"code": "content_mismatch", "message": "Bytes differ."}
    controls.collector.finish(
        entries["calibration-diagnostics"]["submission_id"], "succeeded", result={}
    )
    controls.collector.finish(
        entries["orrery-inputs"]["submission_id"], "rejected", rejection=rejection
    )

    check_after(controls, FIRST_CHECK_SECONDS)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "rejected",
        "content_mismatch",
    )
    assert {entry["kind"]: entry["status"] for entry in receipt["submissions"]} == {
        "calibration-diagnostics": "succeeded",
        "orrery-inputs": "rejected",
    }


def test_a_submission_the_collector_lost_is_created_again(submitting_module):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    del controls.collector.submissions[submission_id]

    check_after(controls, FIRST_CHECK_SECONDS)
    lost = collector_receipt(controls, request)
    check_after(controls, 300)

    assert (lost["status"], lost["error_code"]) == ("pending", "SUBMISSION_NOT_FOUND")
    assert len(controls.collector.created) == 2
    assert collector_receipt(controls, request)["status"] == "awaiting_final_status"


def test_status_checks_resume_in_the_next_emitter_on_the_machine(
    module_factory, collector
):
    """Status checks resume in the next emitter started on the machine.

    A build's emitter can exit before the collector's worker finishes, as a Modal
    container does at the end of a build. The submission stays in the hand-over
    database, and the next emitter started on the machine with the same database
    checks it.
    """
    first = module_factory.start(
        collector_submissions=True, development_collector_url=collector.origin
    )
    try:
        request, submission_id = accepted_bundle(first)
    finally:
        first.module.close(0)
    collector.finish(submission_id, "succeeded", result={"verified_files": 2})

    second = module_factory.start(
        collector_submissions=True, development_collector_url=collector.origin
    )
    try:
        second.clock.advance(FIRST_CHECK_SECONDS)
        second.module.collector_delivery.run_cycle()
        receipt = collector_receipt(second, request)
    finally:
        second.module.close(0)

    assert receipt["status"] == "succeeded"
    [entry] = receipt["submissions"]
    assert entry["receipt"] == collector.receipt_document(submission_id)


def test_hugging_face_commits_land_during_a_held_status_check(
    module_with_workers, monkeypatch
):
    """Status checks run on the collector's worker.

    A slow one never delays a Hugging Face commit, and the build's own calls
    answer at once.
    """
    from microcosm_emitter.submissions import delivery as delivery_module

    monkeypatch.setattr(
        delivery_module, "FIRST_STATUS_CHECK_SECONDS", 0.05, raising=False
    )
    controls = module_with_workers
    controls.hub.create_repository(STAGING_REPOSITORY, {"README.md": b"staging"})
    controls.collector.hold_status_checks()
    request = bundle(controls)
    try:
        assert stage(controls, request)["status"] == "pending"
        assert controls.collector.status_check_started.wait(10), (
            "no status check started"
        )

        stage_progress(controls, flush=True)

        assert wait_until(
            lambda: staging_holds(controls, "runs/staging-run-1/progress.json"),
            seconds=5,
        )
        receipt = collector_receipt(controls, request)
        assert receipt["status"] == "awaiting_final_status"
    finally:
        controls.collector.release_status_checks()


# A submission that ends without success is reported ------------------------
#
# The emitter host is the build's child process and shares its standard
# error, so a line there reaches the build's log.


def report_lines(capsys, handover_id):
    return [
        line for line in capsys.readouterr().err.splitlines() if handover_id in line
    ]


@pytest.mark.parametrize(
    ("final_status", "code"),
    [("rejected", "content_mismatch"), ("failed", "worker_error")],
)
def test_a_submission_that_did_not_succeed_is_reported_once(
    submitting_module, capsys, final_status, code
):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    rejection = {"code": code, "message": f"The worker reported {code}."}
    controls.collector.finish(submission_id, final_status, rejection=rejection)

    check_after(controls, FIRST_CHECK_SECONDS)
    check_after(controls, 3600)

    [line] = report_lines(capsys, request.handover_id())
    for expected in (final_status, code, "calibration-diagnostics", submission_id):
        assert expected in line, line


def test_a_submission_the_collector_refuses_is_reported(submitting_module, capsys):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.collector.create_statuses = [422]

    cycle(controls)

    [line] = report_lines(capsys, request.handover_id())
    assert "failed" in line and "COLLECTOR_REFUSED_422" in line, line


def test_a_succeeded_submission_is_not_reported(submitting_module, capsys):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    controls.collector.finish(submission_id, "succeeded", result={})

    check_after(controls, FIRST_CHECK_SECONDS)

    assert collector_receipt(controls, request)["status"] == "succeeded"
    assert report_lines(capsys, request.handover_id()) == []


# The overall time limit on status checks -----------------------------------
#
# The collector's recovery brings every job to a final status within three
# attempts or dispatches of at most about an hour each. The emitter checks
# for a day after acceptance; a submission still not final then is recorded
# as timed out, and reported.

DAY_SECONDS = 24 * 3600


def status_checks_until_past_the_day(controls):
    """Run cycles until 15 minutes past a day after acceptance.

    The cycles run a minute apart, then a second apart from 15 minutes before the
    day. Returns when each status check ran, in seconds after acceptance.
    """
    accepted_at = controls.clock.now
    checked_at = []

    def run_after(seconds):
        check_after(controls, seconds)
        while len(checked_at) < len(controls.collector.status_requests):
            checked_at.append(controls.clock.now - accepted_at)

    while controls.clock.now - accepted_at < DAY_SECONDS - 900:
        run_after(60)
    while controls.clock.now - accepted_at < DAY_SECONDS + 900:
        run_after(1)
    return checked_at


@pytest.mark.parametrize("answer", ["not final", "outage"])
def test_a_submission_not_final_within_a_day_times_out(
    submitting_module, capsys, answer
):
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    if answer == "outage":
        controls.collector.status_answers = [503] * 1000

    checked_at = status_checks_until_past_the_day(controls)
    checks = len(controls.collector.status_requests)
    check_after(controls, 3600)

    # The last check falls on the limit itself, not up to ten minutes late.
    assert checked_at[-1] == DAY_SECONDS
    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "timed_out",
        "FINAL_STATUS_TIMED_OUT",
    )
    [entry] = receipt["submissions"]
    assert (entry["status"], entry["receipt"]) == ("timed_out", None)
    # The status completion reported; an outage leaves it the last one seen.
    assert entry["collector_status"] == "queued"
    # Never checked again once timed out.
    assert len(controls.collector.status_requests) == checks
    [line] = report_lines(capsys, request.handover_id())
    assert "timed_out" in line and submission_id in line, line


@pytest.mark.parametrize(
    ("request_name", "collector_status"),
    [("status", "queued"), ("receipt", "succeeded")],
)
def test_checks_refused_to_another_user_time_out_with_that_refusal(
    submitting_module, capsys, request_name, collector_status
):
    """Checks refused to another user time out with that refusal as the code.

    Every check answered 403 ``submission_owned_by_another_user`` (the emitters
    running that day have another Hugging Face user's token than the one that
    submitted it): checks go on until the day ends, and the timed-out submission
    keeps the refusal as its code. The collector has the submission and may have
    finished it, so ``FINAL_STATUS_TIMED_OUT`` alone would hide why no final
    status was stored.
    """
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    refusals = [(403, "submission_owned_by_another_user")] * 1000
    if request_name == "status":
        controls.collector.status_answers = refusals
    else:
        controls.collector.finish(submission_id, "succeeded", result={})
        controls.collector.receipt_answers = refusals

    checked_at = status_checks_until_past_the_day(controls)
    checks = len(controls.collector.status_requests)
    check_after(controls, 3600)

    assert checked_at[-1] == DAY_SECONDS
    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "timed_out",
        "COLLECTOR_REFUSED_403",
    )
    [entry] = receipt["submissions"]
    assert (
        entry["status"],
        entry["error_code"],
        entry["collector_status"],
        entry["receipt"],
    ) == ("timed_out", "COLLECTOR_REFUSED_403", collector_status, None)
    assert len(controls.collector.status_requests) == checks
    [line] = report_lines(capsys, request.handover_id())
    assert "timed_out" in line and "COLLECTOR_REFUSED_403" in line, line


def test_a_final_status_found_after_a_long_absence_is_still_stored(
    submitting_module,
):
    """An emitter started days later checks once more before the limit applies.

    A result the collector holds is therefore never thrown away.
    """
    controls = submitting_module
    request, submission_id = accepted_bundle(controls)
    controls.collector.finish(submission_id, "succeeded", result={})

    check_after(controls, 3 * DAY_SECONDS)

    assert collector_receipt(controls, request)["status"] == "succeeded"


# What a build reads ---------------------------------------------------------


class ModuleTransport:
    """Answers an interface client's queries with the module, in this thread.

    It answers them as the emitter host would over its socket.
    """

    def __init__(self, module) -> None:
        self.module = module

    def send(self, module, message, *, timeout=None):
        error_message = "collector receipts are queries"
        raise AssertionError(error_message)

    def query(self, module, message, *, timeout=None):
        assert module == "huggingface"
        return self.module.query(dict(message))

    def close(self) -> None:
        pass


def build_client(controls):
    """Return the interface client a build holds, answered by this module."""
    from pathlib import Path

    from microcosm_emitter_interface.client import EmitterClient
    from microcosm_emitter_interface.launcher import EmitterProcess

    return EmitterClient(
        EmitterProcess(
            process=None,
            transport=ModuleTransport(controls.module),
            runtime_directory=Path("/nonexistent"),
            description={"modules": ["huggingface"], "protocol_versions": [1]},
        )
    )


def test_a_build_reads_each_state_of_its_submissions_at_once(submitting_module):
    """A build reads each state of its submissions at once.

    The build asks; the answer comes from the queue without waiting for any
    status check.
    """
    controls = submitting_module
    client = build_client(controls)
    request, submission_id = accepted_bundle(controls)
    awaiting = client.collector_submission_receipt(request.handover_id())
    rejection = {"code": "content_mismatch", "message": "Bytes differ."}
    controls.collector.finish(submission_id, "rejected", rejection=rejection)
    check_after(controls, FIRST_CHECK_SECONDS)

    rejected = client.collector_submission_receipt(request.handover_id())

    assert (awaiting["status"], awaiting["submissions"][0]["collector_status"]) == (
        "awaiting_final_status",
        "queued",
    )
    assert (rejected["status"], rejected["error_code"]) == (
        "rejected",
        "content_mismatch",
    )
    [entry] = rejected["submissions"]
    assert entry["rejection"] == rejection
    assert entry["receipt"]["submission_id"] == submission_id


def test_a_build_reads_nothing_when_submissions_are_off(default_module):
    controls = default_module
    request = bundle(controls)
    stage(controls, request)

    assert (
        build_client(controls).collector_submission_receipt(request.handover_id())
        is None
    )

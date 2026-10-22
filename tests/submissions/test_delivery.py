"""Bundles reach the collector after their Hugging Face commit, when switched on."""

import hashlib
import threading
import time

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY, STAGING_REPOSITORY

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
    assert receipt["status"] == "submitted"
    assert receipt["error_code"] is None
    [submitted] = receipt["submissions"]
    assert submitted["kind"] == "calibration-diagnostics"
    assert submitted["status"] == "accepted"
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
    assert collector_receipt(controls, request)["status"] == "submitted"


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
    assert collector_receipt(controls, request)["status"] == "submitted"


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
    assert collector_receipt(controls, request)["status"] == "submitted"


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
    assert collector_receipt(controls, request)["status"] == "submitted"


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

    assert collector_receipt(controls, request)["status"] == "submitted"
    assert sorted(controls.collector.objects.values()) == [
        b'{"outputs": {}}',
        b'{"schema": 8}',
    ]


def test_a_folder_longer_than_the_collector_accepts_is_not_submitted(
    submitting_module,
):
    """The contract allows a folder of at most 512 characters; a longer one
    would be refused with a final 422."""
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
    """Hand over a bundle naming Orrery inputs; return it once its first
    upload to the collector has started and is held."""
    controls.hub.create_repository(STAGING_REPOSITORY, {"README.md": b"staging"})
    controls.collector.hold_uploads()
    request = bundle(controls, contents={**CONTENTS, **ORRERY_CONTENTS})
    stage(controls, request)
    assert controls.collector.upload_started.wait(10), "no upload started"
    return request


@pytest.fixture
def module_with_workers(module_factory, collector):
    """The module with its own delivery workers, as the host runs it."""
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
        except BaseException as error:
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
    """The #27 fix review's case. At shutdown a bundle upload holds the
    staging repository, so the first shutdown commit skips its run files;
    that upload finishes in time, while a collector upload is still held
    past the deadline. The second shutdown commit must still send them."""
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
        raise RuntimeError("database is locked")

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
        raise RuntimeError("synthetic")

    controls.module.collector_delivery.client.submit = unexpected
    controls.module.collector_delivery.run_cycle()

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == ("pending", "LOCAL_ERROR")
    # Retried after the usual backoff, not after the hour-long lease.
    controls.clock.advance(5)
    del controls.module.collector_delivery.client.submit
    assert controls.module.collector_delivery.run_cycle() == 1
    assert collector_receipt(controls, request)["status"] == "submitted"


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
    """A second emitter's collector client: records each submission it is
    asked to make, and sends nothing."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.submitted: list[str] = []

    def submit(self, planned):
        from microcosm_emitter.submissions.client import SubmissionOutcome

        self.submitted.append(planned.kind)
        return SubmissionOutcome("pending", "UPLOAD_FAILED")


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
    """The first emitter runs its own workers; the second is a collector
    delivery on the same hand-over database, driven by the test."""
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
    """The #27 round-2 review's NEW BUGS 2: the first emitter's upload is
    held for longer than two leases."""
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
        lambda: collector_receipt(first, request)["status"] == "submitted",
        seconds=10,
    ), "the first emitter's result was not recorded"


def test_a_collector_row_is_free_within_one_lease_after_shutdown(two_emitters):
    """Shutdown abandons the held upload; its lease must no longer be kept,
    so another emitter can take the row over."""
    first, second, second_client = two_emitters
    described_bundle_with_a_held_upload(first)

    first.module.close(time.monotonic() + 1)

    assert wait_until(
        lambda: second.run_cycle() > 0,
        seconds=SHORT_SUBMISSION_LEASE_SECONDS * 3,
    ), "the collector row stayed leased after shutdown"
    assert second_client.submitted

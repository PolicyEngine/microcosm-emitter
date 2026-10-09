"""Durable graph jobs use fake HTTP, independently of the visualization app."""

import json
from dataclasses import FrozenInstanceError

import httpx
import pytest
from microcosm_provider_orrery.contracts import ClaimedGraphPublication
from microcosm_provider_orrery.service.queue import (
    GraphPublicationDelivery,
    GraphPublicationQueue,
    inventory_digest,
    publication_inventory,
)


def source(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"fixture":true}')
    return directory, publication_inventory(directory, {"graph.orrery.json": "graph"})


def test_jobs_survive_restart_without_run_registration_or_event_retention(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    restarted = GraphPublicationQueue(queue.path)
    job = restarted.claim()
    assert isinstance(job, ClaimedGraphPublication)
    assert job.inventory == inventory
    assert "run_id" not in inventory
    assert job.publication_id == inventory["publication_id"]
    assert job.directory == directory.resolve()
    assert restarted.claim() is None


def test_claim_is_a_frozen_record_and_expired_leases_can_be_reclaimed(
    tmp_path, monkeypatch
):
    from microcosm_provider_orrery.service.queue import time

    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    claimed = queue.claim(lease_seconds=5)
    assert isinstance(claimed, ClaimedGraphPublication)
    assert claimed.lease_until == 1005.0
    with pytest.raises(FrozenInstanceError):
        claimed.lease_until = 0
    assert GraphPublicationQueue(queue.path).claim() is None
    clock[0] = 1005.0
    replacement = GraphPublicationQueue(queue.path).claim()
    assert replacement.publication_id == claimed.publication_id
    assert replacement.lease_until > claimed.lease_until


def test_claim_respects_retry_delay_and_published_jobs_are_not_reclaimed(
    tmp_path, monkeypatch
):
    from microcosm_provider_orrery.service.queue import time

    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    claimed = queue.claim()
    queue.finish(
        claimed.publication_id,
        error_code="publication_network_error",
        expected_lease=claimed.lease_until,
    )
    assert queue.claim() is None
    clock[0] = 1002.0
    retried = queue.claim()
    assert retried.publication_id == claimed.publication_id
    queue.finish(
        retried.publication_id,
        url="https://runs.example/runs/published/",
        expected_lease=retried.lease_until,
    )
    assert queue.claim() is None


def test_queue_rejects_changed_bytes_and_conflicting_inventory(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    (directory / "graph.orrery.json").write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        queue.enqueue(directory, inventory)
    with pytest.raises(ValueError):
        publication_inventory(directory, {"private.h5": "graph"})


def test_delivery_retries_missing_credentials_and_network_failure(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    delivery = GraphPublicationDelivery(
        queue, credential=lambda: ("missing", None), origin="https://runs.example"
    )
    delivery.flush_once()
    assert queue.receipt(inventory["publication_id"])["status"] == "pending"
    queue.retry(inventory["publication_id"])
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("secret", request=request)
            )
        )
    )
    GraphPublicationDelivery(
        queue,
        credential=lambda: ("ok", "session"),
        origin="https://runs.example",
        client=client,
    ).flush_once()
    receipt = queue.receipt(inventory["publication_id"])
    assert receipt["status"] == "pending"
    assert "secret" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://store.blob.vercel-storage.com/upload?signed=credential",
        "https://vercel.com/api/blob/?pathname=object&signed=credential",
    ],
)
def test_delivery_uses_exact_bytes_and_does_not_forward_session_to_signed_upload(
    tmp_path,
    upload_url,
):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/uploads/":
            return httpx.Response(
                200,
                json={
                    "uploads": [
                        {
                            "name": "graph.orrery.json",
                            "url": upload_url,
                            "already_uploaded": False,
                        }
                    ]
                },
            )
        if request.method == "PUT":
            assert "authorization" not in request.headers
            assert request.content == (directory / "graph.orrery.json").read_bytes()
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "publication_id": inventory["publication_id"],
                "inventory_sha256": inventory_digest(inventory),
            },
        )

    delivery = GraphPublicationDelivery(
        queue,
        credential=lambda: ("ok", "session"),
        origin="https://runs.example",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    delivery.flush_once()
    receipt = queue.receipt(inventory["publication_id"])
    assert receipt["status"] == "published"
    assert len(requests) == 3
    assert "signed=" not in json.dumps(receipt)
    assert "session" not in json.dumps(receipt)


def test_queue_resumes_after_worker_exits_and_keeps_pending_jobs(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    queue.claim(lease_seconds=0)
    assert GraphPublicationQueue(queue.path).claim() is not None


def test_late_failed_attempt_cannot_replace_a_published_receipt(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    queue.claim(lease_seconds=0)
    replacement = GraphPublicationQueue(queue.path)
    replacement.claim()
    id_ = inventory["publication_id"]
    replacement.finish(id_, url=f"https://runs.example/runs/{id_}/")
    published = queue.receipt(id_)
    queue.finish(id_, error_code="publication_network_error")
    assert queue.receipt(id_) == published
    assert json.loads((directory / "publication.status.json").read_bytes()) == published


def test_expired_worker_cannot_release_a_replacement_workers_lease(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    expired = queue.claim(lease_seconds=0)
    replacement = GraphPublicationQueue(queue.path)
    current = replacement.claim()
    id_ = inventory["publication_id"]
    initial = queue.receipt(id_)
    queue.finish(
        id_,
        error_code="publication_network_error",
        expected_lease=expired.lease_until,
    )
    assert queue.receipt(id_) == initial
    assert queue.claim() is None
    replacement.finish(
        id_,
        url=f"https://runs.example/runs/{id_}/",
        expected_lease=current.lease_until,
    )
    assert queue.receipt(id_)["status"] == "published"


def test_telemetry_pruning_cannot_remove_graph_jobs(tmp_path):
    from microcosm_provider_telemetry.service.spool import EventSpool

    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    EventSpool(queue.path).prune()
    assert (
        GraphPublicationQueue(queue.path).receipt(inventory["publication_id"])["status"]
        == "pending"
    )


def test_rejected_graph_session_is_refreshed_without_discarding_the_job(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    invalidations = []
    delivery = GraphPublicationDelivery(
        queue,
        credential=lambda: ("ok", "expired"),
        invalidate_credential=lambda: invalidations.append(True),
        origin="https://runs.example",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(401))
        ),
    )
    assert not delivery.flush_once()
    assert invalidations == [True]
    assert queue.receipt(inventory["publication_id"])["status"] == "pending"


def test_retry_cli_reuses_identity_and_leaves_hf_snapshot_unchanged(
    tmp_path, monkeypatch
):
    from microcosm_provider_orrery import cli

    directory, inventory = source(tmp_path)
    (directory / "orrery.upload.json").write_text(json.dumps(inventory))
    snapshot = directory / "orrery.publication.json"
    snapshot.write_text('{"status":"pending"}')
    calls = []

    class Delivery:
        def __init__(self, queue, **kwargs):
            self.queue = queue

        def flush_once(self, *, publication_id):
            calls.append(publication_id)
            self.queue.finish(
                publication_id, url="https://runs.example/runs/published/"
            )

    monkeypatch.setattr(cli, "GraphPublicationDelivery", Delivery)
    assert (
        cli.main(
            [
                "--spool",
                str(tmp_path / "jobs.sqlite3"),
                "--publication-id",
                inventory["publication_id"],
                "--directory",
                str(directory),
            ]
        )
        == 0
    )
    assert calls == [inventory["publication_id"]]
    assert snapshot.read_text() == '{"status":"pending"}'


def test_deleted_preserved_directory_is_retained_without_stopping_delivery(
    tmp_path, monkeypatch
):
    import shutil

    from microcosm_provider_orrery.service.queue import time

    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    (tmp_path / "removed").mkdir()
    (tmp_path / "kept").mkdir()
    removed, removed_inventory = source(tmp_path / "removed")
    queue.enqueue(removed, removed_inventory)
    clock[0] = 1001.0
    kept, kept_inventory = source(tmp_path / "kept")
    queue.enqueue(kept, kept_inventory)
    shutil.rmtree(removed)
    delivery = GraphPublicationDelivery(
        queue, credential=lambda: ("missing", None), origin="https://runs.example"
    )

    assert delivery.flush_once() is False
    receipt = queue.receipt(removed_inventory["publication_id"])
    assert receipt["status"] == "pending"
    assert receipt["error_code"] == "preserved_files_missing"
    assert receipt["attempts"] == 1
    # The failed job backs off, so the next claim reaches the other job.
    assert queue.claim().publication_id == kept_inventory["publication_id"]


def test_status_file_failure_does_not_roll_back_the_queue_result(tmp_path):
    directory, inventory = source(tmp_path)
    queue = GraphPublicationQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(directory, inventory)
    claimed = queue.claim()
    (directory / "publication.status.json").mkdir()
    queue.finish(
        claimed.publication_id,
        url="https://runs.example/runs/published/",
        expected_lease=claimed.lease_until,
    )
    assert queue.receipt(claimed.publication_id)["status"] == "published"
    assert list(directory.glob("*.tmp")) == []


def test_orrery_module_survives_a_local_queue_error(capsys):
    from microcosm_provider_orrery.service.module import OrreryModule
    from sqlalchemy.exc import OperationalError

    attempts = []

    class Delivery:
        def flush_once(self):
            attempts.append(None)
            if len(attempts) == 1:
                raise OperationalError("UPDATE", {}, Exception("database is locked"))
            return False

    module = OrreryModule({}, None)
    module.delivery = Delivery()
    module.tick(0.0)
    module.tick(1.0)
    module.tick(2.0)
    assert len(attempts) == 3
    assert capsys.readouterr().err.count("local queue error") == 1

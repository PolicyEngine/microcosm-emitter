"""Durable, retryable immutable graph uploads owned by the emitter service.

Graph bytes stay in preserved files, never in event details. Pending graph jobs
are not subject to the telemetry event queue's time or size pruning.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from microcosm_provider_core.database import (
    create_spool_engine,
    create_spool_session_factory,
)
from microcosm_provider_core.migrations import upgrade_spool_database
from microcosm_provider_core.models import GraphPublicationJob
from sqlalchemy import select, update

MAX_GRAPH_FILE_BYTES = 64 * 1024 * 1024
MAX_GRAPH_TOTAL_BYTES = 512 * 1024 * 1024
GRAPH_PUBLICATION_ACTION = "graph_publication"
GRAPH_PUBLICATION_WAIT_SECONDS = 30.0
GRAPH_PUBLICATION_LEASE_SECONDS = 300.0
GRAPH_PUBLICATION_STATUS_FILENAME = "publication.status.json"
DEFAULT_GRAPH_PUBLICATION_ORIGIN = "https://microcosm-runs.vercel.app"
GRAPH_PUBLICATION_ROLES = {
    "graph": r"graph\.orrery\.json",
    "schema": r"graph\.schema\.json",
    "index": r"execution\.evidence\.json",
    "declaration": r"evidence-[A-Za-z0-9_-]+-graph\.json",
    "manifest": r"evidence-[A-Za-z0-9_-]+-manifest\.json",
    "binding": r"evidence-[A-Za-z0-9_-]+-binding\.json",
    "summaries": r"evidence-[A-Za-z0-9_-]+-summaries\.json",
    "upstream": r"upstream-[a-f0-9]{64}\.json",
}


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def inventory_digest(inventory: Mapping) -> str:
    """Digest the versioned field order shared with the publication API."""
    inventory = {
        "version": inventory["version"],
        "publication_id": inventory["publication_id"],
        "files": [
            {key: item[key] for key in ("name", "role", "bytes", "sha256")}
            for item in sorted(inventory["files"], key=lambda file: file["name"])
        ],
    }
    return _digest(
        json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode()
    )


def publication_inventory(
    directory: Path, roles: Mapping[str, str], *, publication_id: str | None = None
) -> dict:
    """Hash an explicit allowlist of files; publication identity is not a run ID."""
    files = []
    for name, role in sorted(roles.items()):
        if role not in GRAPH_PUBLICATION_ROLES or not re.fullmatch(
            GRAPH_PUBLICATION_ROLES[role], name
        ):
            raise ValueError("Ineligible graph publication filename or role.")
        path = Path(directory) / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                "Graph publication files must be regular, preserved files."
            )
        size = path.stat().st_size
        if not 0 < size <= MAX_GRAPH_FILE_BYTES:
            raise ValueError("Graph publication file exceeds its size limit.")
        files.append(
            {
                "name": name,
                "role": role,
                "bytes": size,
                "sha256": _digest(path.read_bytes()),
            }
        )
    if (
        not files
        or len(files) > 256
        or sum(f["bytes"] for f in files) > MAX_GRAPH_TOTAL_BYTES
        or sum(f["role"] == "graph" for f in files) != 1
    ):
        raise ValueError("Invalid graph publication inventory.")
    id_ = publication_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", id_):
        raise ValueError("Invalid graph publication ID.")
    return {"version": 1, "publication_id": id_, "files": files}


class GraphPublicationQueue:
    """SQLite job records with leases; retained until publication or operator action."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_spool_engine(self.path)
        upgrade_spool_database(self._engine)
        self._sessions = create_spool_session_factory(self._engine)

    def enqueue(self, directory: Path, inventory: dict) -> None:
        """Durably register exact bytes without a telemetry run or credential."""
        directory = Path(directory).resolve()
        expected = publication_inventory(
            directory,
            {f["name"]: f["role"] for f in inventory["files"]},
            publication_id=inventory["publication_id"],
        )
        if expected != inventory:
            raise ValueError("Graph publication digest mismatch.")
        id_ = inventory["publication_id"]
        with self._sessions.begin() as session:
            existing = session.get(GraphPublicationJob, id_)
            if existing is not None:
                if existing.inventory != inventory or existing.directory != str(
                    directory
                ):
                    raise ValueError(
                        "Graph publication identity conflicts with a queued job."
                    )
                return
            session.add(
                GraphPublicationJob(
                    publication_id=id_,
                    inventory=inventory,
                    directory=str(directory),
                    status="pending",
                    attempts=0,
                    next_attempt_at=0,
                    lease_until=0,
                    receipt={
                        "version": 1,
                        "publication_id": id_,
                        "status": "pending",
                        "error_code": None,
                    },
                )
            )

    def close(self) -> None:
        """Release this handle's database connections without deleting jobs."""
        self._engine.dispose()

    def claim(
        self,
        *,
        lease_seconds: float = GRAPH_PUBLICATION_LEASE_SECONDS,
        publication_id: str | None = None,
    ) -> dict | None:
        """Atomically lease one eligible job across concurrent emitter processes."""
        now = time.time()
        with self._sessions.begin() as session:
            statement = select(GraphPublicationJob.publication_id)
            if publication_id is not None:
                statement = statement.where(
                    GraphPublicationJob.publication_id == publication_id
                )
            id_ = session.scalar(
                statement.where(
                    GraphPublicationJob.status == "pending",
                    GraphPublicationJob.next_attempt_at <= now,
                    GraphPublicationJob.lease_until <= now,
                ).limit(1)
            )
            if id_ is None:
                return None
            result = session.execute(
                update(GraphPublicationJob)
                .where(
                    GraphPublicationJob.publication_id == id_,
                    GraphPublicationJob.status == "pending",
                    GraphPublicationJob.lease_until <= now,
                )
                .values(lease_until=now + lease_seconds)
            )
            if not result.rowcount:
                return None
            job = session.get(GraphPublicationJob, id_)
            return {
                "publication_id": id_,
                "directory": job.directory,
                "inventory": job.inventory,
                "lease_until": job.lease_until,
            }

    def receipt(self, id_: str) -> dict | None:
        """Read the latest local result; old HF receipt snapshots remain unchanged."""
        with self._sessions() as session:
            job = session.get(GraphPublicationJob, id_)
            return None if job is None else dict(job.receipt)

    def finish(
        self,
        id_: str,
        *,
        error_code: str | None = None,
        url: str | None = None,
        expected_lease: float | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            statement = update(GraphPublicationJob).where(
                GraphPublicationJob.publication_id == id_,
                GraphPublicationJob.status == "pending",
            )
            if expected_lease is not None:
                statement = statement.where(
                    GraphPublicationJob.lease_until == expected_lease
                )
            job = session.scalar(
                statement.values(
                    attempts=GraphPublicationJob.attempts + 1,
                    status="published" if url else "pending",
                    lease_until=0,
                ).returning(GraphPublicationJob)
            )
            # Completion is conditional in SQL, not a read-then-write check:
            # expired workers cannot release another lease or replace success.
            if job is None:
                return
            job.next_attempt_at = time.time() + min(300, 2 ** min(job.attempts, 8))
            job.receipt = {
                "version": 1,
                "publication_id": id_,
                "status": job.status,
                "error_code": error_code,
                "url": url,
                "attempts": job.attempts,
                "inventory_sha256": inventory_digest(job.inventory),
            }
            # This is a separate latest-result file, not the build/HF receipt.
            path = Path(job.directory) / GRAPH_PUBLICATION_STATUS_FILENAME
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(job.receipt, sort_keys=True) + "\n")
            temporary.replace(path)

    def retry(self, id_: str | None = None) -> None:
        """Make preserved pending jobs eligible again, including missing credentials."""
        statement = update(GraphPublicationJob).where(
            GraphPublicationJob.status == "pending"
        )
        if id_ is not None:
            statement = statement.where(GraphPublicationJob.publication_id == id_)
        with self._sessions.begin() as session:
            session.execute(statement.values(next_attempt_at=0))


class GraphPublicationDelivery:
    """Upload exact files with scoped signed URLs, then seal the publication."""

    def __init__(
        self,
        queue: GraphPublicationQueue,
        *,
        credential: Callable[[], tuple[str, str | None]],
        origin: str,
        client: httpx.Client | None = None,
        invalidate_credential: Callable[[], None] | None = None,
    ):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "Graph publication origin must be HTTPS or local development."
            )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Graph publication destination must be an origin.")
        self.queue, self.credential, self.origin = queue, credential, origin.rstrip("/")
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.invalidate_credential = invalidate_credential

    def flush_once(self, *, publication_id: str | None = None) -> bool:
        """Try one job; failures retain it without changing telemetry eligibility."""
        job = self.queue.claim(publication_id=publication_id)
        if job is None:
            return False
        id_, inventory, directory = (
            job["publication_id"],
            job["inventory"],
            Path(job["directory"]),
        )
        error = "publication_unavailable"
        lease = job["lease_until"]
        try:
            status, token = self.credential()
            if token is None:
                self.queue.finish(
                    id_, error_code=f"credential_{status}", expected_lease=lease
                )
                return False
            current = publication_inventory(
                directory,
                {f["name"]: f["role"] for f in inventory["files"]},
                publication_id=id_,
            )
            if current != inventory:
                raise ValueError("preserved_bytes_changed")
            headers = {"Authorization": f"Bearer {token}"}
            response = self.client.post(
                self.origin + "/api/uploads/", json=inventory, headers=headers
            )
            response.raise_for_status()
            body = response.json()
            uploads = body.get("uploads")
            if not isinstance(uploads, list):
                raise ValueError("invalid_upload_response")
            files = {f["name"]: f for f in inventory["files"]}
            for upload in uploads:
                name = upload.get("name")
                if name not in files:
                    raise ValueError("invalid_upload_response")
                if upload.get("already_uploaded"):
                    continue
                url = urlsplit(upload["url"])
                if (
                    url.scheme != "https"
                    or not (
                        (url.hostname == "vercel.com" and url.path == "/api/blob/")
                        or url.hostname == "blob.vercel-storage.com"
                        or (url.hostname or "").endswith(".blob.vercel-storage.com")
                    )
                    or url.username
                    or url.password
                ):
                    raise ValueError("invalid_upload_url")
                with (directory / name).open("rb") as stream:
                    result = self.client.put(
                        upload["url"],
                        content=stream,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": str(files[name]["bytes"]),
                        },
                    )
                result.raise_for_status()
            response = self.client.post(
                self.origin + "/api/publications/", json=inventory, headers=headers
            )
            response.raise_for_status()
            result = response.json()
            if result.get("publication_id") != id_ or result.get(
                "inventory_sha256"
            ) != inventory_digest(inventory):
                raise ValueError("invalid_publication_response")
            self.queue.finish(
                id_, url=f"{self.origin}/runs/{id_}/", expected_lease=lease
            )
            return True
        except httpx.HTTPStatusError as exception:
            error = f"publication_http_{exception.response.status_code}"
            if exception.response.status_code == 401 and self.invalidate_credential:
                self.invalidate_credential()
        except httpx.HTTPError:
            error = "publication_network_error"
        except (OSError, ValueError, KeyError, TypeError):
            error = "publication_invalid_evidence_or_response"
        self.queue.finish(id_, error_code=error, expected_lease=lease)
        return False

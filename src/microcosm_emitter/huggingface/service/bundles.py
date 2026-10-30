"""Stage one dataset bundle: the transport of Microcosm's ``stage_bundle``.

The behaviour follows ``staging_dataset.stage_bundle``: one commit of every
file under ``<prefix>/<run_id>/``, pinned to the head it checked, and
idempotent on the outputs' digests. Two things change because the work is now
queued: transient Hub failures are retried instead of ending the stage, and a
commit that loses a race to another writer is checked and tried again.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from microcosm_emitter.huggingface.service.hub import (
    MAX_PARENT_CONFLICT_ATTEMPTS,
    PRESERVED_BYTES_CHANGED,
    PRESERVED_FILES_MISSING,
    REPOSITORY_UNAVAILABLE,
    UPLOAD_FAILED,
    DatasetRepository,
    add_operation,
    is_parent_conflict,
)
from microcosm_emitter.huggingface.service.queue import sha256_file
from microcosm_emitter_interface.bundles import BUNDLE_INVENTORY_ROLE, SIDECAR_ROLES

REMOTE_DIFFERS = "REMOTE_DIFFERS"


type BundleStatus = Literal["pending", "uploaded", "already_staged", "failed"]


@dataclass(frozen=True)
class BundleOutcome:
    """One attempt's result: final unless ``retry``."""

    status: BundleStatus
    error_code: str | None = None
    revision: str | None = None
    retry: bool = False
    committed: bool = False


def _same_digests(remote: object, local: Mapping[str, str]) -> bool:
    """Microcosm ``_same_digests``: compare the outputs' digests only."""
    if not isinstance(remote, Mapping) or set(remote) != set(local):
        return False
    for name, digest in local.items():
        other = remote.get(name)
        if not isinstance(other, Mapping) or other.get("sha256") != digest:
            return False
    return True


def _local_files_outcome(files: list[Mapping[str, Any]]) -> BundleOutcome | None:
    for entry in files:
        path = Path(entry["local_path"])
        try:
            if not path.is_file():
                return BundleOutcome("pending", PRESERVED_FILES_MISSING, retry=True)
            if (
                path.stat().st_size != entry["bytes"]
                or sha256_file(path) != entry["sha256"]
            ):
                return BundleOutcome("failed", PRESERVED_BYTES_CHANGED)
        except OSError:
            return BundleOutcome("pending", PRESERVED_FILES_MISSING, retry=True)
    return None


def stage_bundle(
    repository: DatasetRepository,
    request: Mapping[str, Any],
    copied: Mapping[str, bytes] | None = None,
) -> BundleOutcome:
    """Write the bundle once, or report why not; never raises for the Hub.

    ``copied`` holds the bytes of the files copied at hand-over; they are
    uploaded as they were then. Every other file is re-hashed from its path
    first and never uploaded if its bytes changed.
    """
    copied = {} if copied is None else dict(copied)
    files = list(request["files"])
    local = _local_files_outcome(
        [entry for entry in files if entry["name"] not in copied]
    )
    if local is not None:
        return local
    remote_prefix = f"{request['prefix']}/{request['run_id']}"
    inventory = next(entry for entry in files if entry["role"] == BUNDLE_INVENTORY_ROLE)
    remote_inventory = f"{remote_prefix}/{inventory['name']}"
    outputs = {
        entry["name"]: entry["sha256"]
        for entry in files
        if entry["role"] not in SIDECAR_ROLES
    }
    message = request.get("commit_message") or (
        f"Stage {request['run_id']} under {remote_prefix}"
    )
    for attempt in range(MAX_PARENT_CONFLICT_ATTEMPTS):
        try:
            exists = repository.file_exists(remote_inventory)
        # stage_bundle never raises for the Hub: any failure reading it leaves
        # the bundle pending for a retry.
        except Exception:  # noqa: BLE001
            return BundleOutcome("pending", REPOSITORY_UNAVAILABLE, retry=True)
        if exists:
            try:
                remote = json.loads(repository.download(remote_inventory))
            # stage_bundle never raises for the Hub: any failure reading it
            # leaves the bundle pending for a retry.
            except Exception:  # noqa: BLE001
                return BundleOutcome("pending", REPOSITORY_UNAVAILABLE, retry=True)
            remote_files = remote.get("files") if isinstance(remote, Mapping) else None
            if not _same_digests(remote_files, outputs):
                return BundleOutcome("failed", REMOTE_DIFFERS)
            # The commit that added the folder (a bundle is never rewritten,
            # so it last changed the inventory), never a later head: the
            # collector's provenance names it. Without it, ask again later.
            try:
                revision = repository.last_commit(remote_inventory)
            # stage_bundle never raises for the Hub: any failure reading the
            # commit counts as no revision, which leaves the bundle pending.
            except Exception:  # noqa: BLE001
                revision = None
            if not revision:
                return BundleOutcome("pending", REPOSITORY_UNAVAILABLE, retry=True)
            return BundleOutcome("already_staged", revision=revision)
        try:
            parent = repository.head_revision()
            revision = repository.commit(
                [
                    add_operation(
                        f"{remote_prefix}/{entry['name']}",
                        copied.get(entry["name"], Path(entry["local_path"])),
                    )
                    for entry in files
                ],
                message=message,
                parent_commit=parent,
            )
        # stage_bundle never raises for the Hub: a failed commit is tried again
        # after a parent conflict, and otherwise leaves the bundle pending for
        # a retry.
        except Exception as error:  # noqa: BLE001
            if is_parent_conflict(error) and attempt + 1 < MAX_PARENT_CONFLICT_ATTEMPTS:
                continue
            return BundleOutcome("pending", UPLOAD_FAILED, retry=True)
        return BundleOutcome("uploaded", revision=revision, committed=True)
    return BundleOutcome("pending", UPLOAD_FAILED, retry=True)  # pragma: no cover

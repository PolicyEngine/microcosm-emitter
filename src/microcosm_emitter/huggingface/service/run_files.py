"""One commit carrying every pending staging run file of a repository.

This is the transport that Microcosm's ``StagingRunBundleWriter`` (staging
contract version 1, ``staging.py``) and ``StagingRunBundleWriterV2``
(``staging_v2.py``) performed with one ``upload_file`` commit per file. The
paths and bytes are those the build handed over. Version 1's ``runs.json``
index is merged here, because the merge needs the repository's current copy.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from microcosm_emitter.huggingface.service.hub import (
    MAX_PARENT_CONFLICT_ATTEMPTS,
    DatasetRepository,
    add_operation,
    is_absent,
    is_parent_conflict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from microcosm_emitter.huggingface.service.queue import PendingRunFile
    from microcosm_emitter_interface.serialization import JsonValue

#: Staging contract version 1's index of runs, at the repository root.
RUNS_INDEX_PATH = "runs.json"
RUNS_INDEX_SCHEMA_VERSION = 1


def _jsonable(value: object) -> JsonValue:
    """Microcosm ``staging._jsonable``: the same conversion before writing."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _jsonable(item())
    return str(value)


def merge_runs_index(
    existing: bytes | None,
    entries: Sequence[Mapping[str, Any]],
    *,
    updated_at: str,
) -> bytes:
    """Upsert each run's summary into ``runs.json`` as Microcosm did.

    Other runs are kept, each entry replaces its own run, and runs are sorted
    newest first by ``updated_at``, then ``started_at``, then ``run_id``. The
    bytes equal ``staging._write_json`` output: one-space indentation, no
    trailing newline.
    """
    runs: list[dict[str, Any]] = []
    if existing is not None:
        try:
            document = json.loads(existing)
        except ValueError:
            document = None
        listed = document.get("runs") if isinstance(document, dict) else None
        if isinstance(listed, list):
            runs = [
                run for run in listed if isinstance(run, dict) and run.get("run_id")
            ]
    for entry in entries:
        runs = [run for run in runs if run.get("run_id") != entry.get("run_id")]
        runs.append(dict(entry))
    runs.sort(
        key=lambda run: str(
            run.get("updated_at") or run.get("started_at") or run.get("run_id")
        ),
        reverse=True,
    )
    return json.dumps(
        _jsonable(
            {
                "schema_version": RUNS_INDEX_SCHEMA_VERSION,
                "updated_at": updated_at,
                "runs": runs,
            }
        ),
        indent=1,
        allow_nan=False,
    ).encode()


def commit_message(
    entries: Sequence[PendingRunFile],
    index_entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> str:
    """Return the commit message: ``Stage run files for`` and the sorted run IDs.

    The run IDs are those of ``entries`` and ``index_entries``, each once,
    separated by commas.
    """
    run_ids = sorted(
        {entry.run_id for entry in entries} | {r for r, _ in index_entries}
    )
    return f"Stage run files for {', '.join(run_ids)}"


def commit_run_files(
    repository: DatasetRepository,
    entries: Sequence[PendingRunFile],
    index_entries: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    updated_at: str,
) -> str:
    """Write the pending files (and merged index) in one commit; return it.

    With an index to merge, the commit is pinned to the head the index was
    read at; when another writer commits first, the index is read again and
    merged again, so neither writer's runs are lost.
    """
    message = commit_message(entries, index_entries)
    for attempt in range(MAX_PARENT_CONFLICT_ATTEMPTS):
        operations = [
            add_operation(entry.path_in_repo, entry.content) for entry in entries
        ]
        parent = None
        if index_entries:
            parent = repository.head_revision()
            try:
                existing: bytes | None = repository.download(
                    RUNS_INDEX_PATH, revision=parent
                )
            except Exception as error:
                # Only a repository without runs.json starts a new index. Any
                # other error retries the cycle: starting empty would drop
                # every other run from the index.
                if not is_absent(error):
                    raise
                existing = None
            operations.append(
                add_operation(
                    RUNS_INDEX_PATH,
                    merge_runs_index(
                        existing,
                        [entry for _, entry in index_entries],
                        updated_at=updated_at,
                    ),
                )
            )
        try:
            return repository.commit(operations, message=message, parent_commit=parent)
        except Exception as error:
            if (
                parent is None
                or not is_parent_conflict(error)
                or attempt + 1 == MAX_PARENT_CONFLICT_ATTEMPTS
            ):
                raise
    error_message = "unreachable"
    raise AssertionError(error_message)  # pragma: no cover

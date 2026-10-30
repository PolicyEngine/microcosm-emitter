"""Check hand-over requests with the interface's own rules, then read their files.

A request that breaks a rule is refused as a whole with a fixed code; nothing
is queued, and no exception text is returned.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS
from microcosm_emitter_interface.bundles import (
    BundleFile,
    StageDatasetBundle,
    handover_identifier,
)
from microcosm_emitter_interface.identifiers import (
    relative_file_path,
    repository_identifier,
    repository_prefix,
    safe_identifier,
)
from microcosm_emitter_interface.publication import (
    RELEASE_CHANGE,
    PromoteBuild,
    PublishRelease,
    ReleaseChange,
    ReleaseFile,
    WithdrawBuild,
)
from microcosm_emitter_interface.submissions import CollectorProvenance

if TYPE_CHECKING:
    from collections.abc import Callable

#: Transport bounds for one staging run file and one hand-over. Microcosm's
#: staging contract keeps its own, smaller limits.
MAX_RUN_FILE_BYTES = 32 * 1024 * 1024
MAX_HAND_OVER_BYTES = 64 * 1024 * 1024
#: Staging contract version 1's pointer to the latest run, at the root.
LATEST_STAGING_POINTER_PATH = "latest_staging.json"
#: Bundle files are copied into the queue when they are handed over, smallest
#: first, each of at most ``MAX_COPIED_FILE_BYTES`` and together at most
#: ``MAX_COPIED_BYTES``; larger files are uploaded from their paths.
MAX_COPIED_FILE_BYTES = 32 * 1024 * 1024
MAX_COPIED_BYTES = 64 * 1024 * 1024


class RequestRefusedError(ValueError):
    """A request the emitter will not queue; ``code`` says why."""

    def __init__(self, code: str) -> None:
        """Store the refusal ``code``, which is also the exception's message."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunFilesHandOver:
    """A checked ``stage_run_files`` request, with the bytes of every file it names.

    ``files`` pairs each repository path with its bytes. ``interval`` is the
    request's ``commit_interval_seconds``, and ``flush`` makes the files due at
    the next cycle.
    """

    repository: str
    run_id: str
    files: list[tuple[str, bytes]]
    run_index_entry: dict[str, Any] | None
    flush: bool
    interval: float


def require_supported_version(message: Mapping[str, Any]) -> None:
    """Refuse a message whose ``protocol_version`` this emitter does not support.

    The refusal is a ``RequestRefusedError`` with ``UNSUPPORTED_PROTOCOL_VERSION``;
    a version that is missing or not an integer is refused too.
    """
    version = message.get("protocol_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_PROTOCOL_VERSIONS
    ):
        refusal_code = "UNSUPPORTED_PROTOCOL_VERSION"
        raise RequestRefusedError(refusal_code)


def _valid(check: Callable[..., str], *arguments: object, **keywords: str) -> str:
    try:
        return check(*arguments, **keywords)
    except ValueError:
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code) from None


def run_reference(message: Mapping[str, Any]) -> tuple[str, str]:
    """Return the repository and run ID a message names, after checking both.

    An unsupported protocol version, or an invalid repository or run ID, raises
    ``RequestRefusedError``.
    """
    require_supported_version(message)
    return (
        _valid(repository_identifier, message.get("repository")),
        _valid(safe_identifier, message.get("run_id"), label="run_id"),
    )


def _read_local_file(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    path = Path(value)
    if not path.is_absolute():
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    try:
        if not path.is_file():
            refusal_code = "LOCAL_FILE_MISSING"
            raise RequestRefusedError(refusal_code)
        if path.stat().st_size > MAX_RUN_FILE_BYTES:
            refusal_code = "FILE_TOO_LARGE"
            raise RequestRefusedError(refusal_code)
        content = path.read_bytes()
    except OSError:
        refusal_code = "LOCAL_FILE_MISSING"
        raise RequestRefusedError(refusal_code) from None
    if len(content) > MAX_RUN_FILE_BYTES:
        refusal_code = "FILE_TOO_LARGE"
        raise RequestRefusedError(refusal_code)
    return content


def parse_stage_run_files(message: Mapping[str, Any]) -> RunFilesHandOver:
    """Validate a ``stage_run_files`` request and read every named file."""
    repository, run_id = run_reference(message)
    prefix = _valid(repository_prefix, message.get("prefix"))
    flush = message.get("flush")
    interval = message.get("commit_interval_seconds")
    if not isinstance(flush, bool) or (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 0
    ):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    entries = message.get("files")
    if not isinstance(entries, list):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    targets: list[tuple[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            refusal_code = "INVALID_REQUEST"
            raise RequestRefusedError(refusal_code)
        relative = _valid(
            relative_file_path, entry.get("relative_path"), label="relative_path"
        )
        targets.append((f"{prefix}/{run_id}/{relative}", entry.get("local_path")))
    pointer = message.get("latest_pointer")
    if pointer is not None:
        targets.append((LATEST_STAGING_POINTER_PATH, pointer))
    if len({path_in_repo for path_in_repo, _ in targets}) != len(targets):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    run_index_entry = message.get("run_index_entry")
    if run_index_entry is not None and (
        not isinstance(run_index_entry, Mapping)
        or run_index_entry.get("run_id") != run_id
    ):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    files = []
    total = 0
    for path_in_repo, local_path in targets:
        content = _read_local_file(local_path)
        total += len(content)
        if total > MAX_HAND_OVER_BYTES:
            refusal_code = "FILE_TOO_LARGE"
            raise RequestRefusedError(refusal_code)
        files.append((path_in_repo, content))
    return RunFilesHandOver(
        repository=repository,
        run_id=run_id,
        files=files,
        run_index_entry=None if run_index_entry is None else dict(run_index_entry),
        flush=flush,
        interval=float(interval),
    )


@dataclass(frozen=True)
class BundleHandOver:
    """A checked ``stage_dataset_bundle`` request and the file bytes copied for it.

    ``request`` is the message as it was handed over, and ``remote_prefix`` the
    bundle's folder in the repository.
    """

    handover_id: str
    repository: str
    remote_prefix: str
    request: dict[str, Any]
    #: The bytes of the files copied at hand-over, by name.
    copied: dict[str, bytes] = field(default_factory=dict)


def copy_small_files(files: Sequence[BundleFile]) -> dict[str, bytes]:
    """Read the smallest files within the copy limits; check each digest.

    A copied file must hold exactly its declared bytes now, since the build
    may rewrite it as soon as the hand-over is answered.
    """
    copied: dict[str, bytes] = {}
    total = 0
    for item in sorted(files, key=lambda item: (item.bytes, item.name)):
        if item.bytes > MAX_COPIED_FILE_BYTES or total + item.bytes > MAX_COPIED_BYTES:
            break  # Every later file is at least as large.
        try:
            content = Path(item.local_path).read_bytes()
        except OSError:
            refusal_code = "LOCAL_FILE_MISSING"
            raise RequestRefusedError(refusal_code) from None
        if len(content) != item.bytes:
            refusal_code = "FILE_SIZE_DIFFERS"
            raise RequestRefusedError(refusal_code)
        if hashlib.sha256(content).hexdigest() != item.sha256:
            refusal_code = "FILE_DIGEST_DIFFERS"
            raise RequestRefusedError(refusal_code)
        copied[item.name] = content
        total += item.bytes
    return copied


COLLECTOR_PROVENANCE_KEYS = frozenset(
    {"country_code", "build_id", "licensed_data", "release"}
)


def _collector_provenance(value: object) -> CollectorProvenance | None:
    """Return the optional build description for the collector, checked like the rest.

    ``None`` stays ``None``. Anything other than a mapping with exactly the
    ``COLLECTOR_PROVENANCE_KEYS`` raises ``ValueError``.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != COLLECTOR_PROVENANCE_KEYS:
        error_message = "collector must name exactly the provenance fields."
        raise ValueError(error_message)
    return CollectorProvenance(**value)


def parse_stage_dataset_bundle(message: Mapping[str, Any]) -> BundleHandOver:
    """Check a bundle with the interface's rules and the files' sizes.

    Small files are copied and their digests checked now (``copy_small_files``);
    hashing the large ones waits for the worker, so the request answers
    quickly.
    """
    require_supported_version(message)
    entries = message.get("files")
    if not isinstance(entries, list) or any(
        not isinstance(entry, Mapping) for entry in entries
    ):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    try:
        request = StageDatasetBundle(
            repository=message.get("repository"),
            run_id=message.get("run_id"),
            prefix=message.get("prefix"),
            commit_message=message.get("commit_message"),
            collector=_collector_provenance(message.get("collector")),
            files=tuple(
                BundleFile(
                    name=entry.get("name"),
                    role=entry.get("role"),
                    local_path=Path(str(entry.get("local_path"))),
                    sha256=entry.get("sha256"),
                    bytes=entry.get("bytes"),
                )
                for entry in entries
            ),
        )
    except (ValueError, TypeError):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code) from None
    for entry, item in zip(entries, request.files, strict=True):
        path = Path(str(entry.get("local_path")))
        if not path.is_absolute():
            refusal_code = "INVALID_REQUEST"
            raise RequestRefusedError(refusal_code)
        try:
            if not path.is_file():
                refusal_code = "LOCAL_FILE_MISSING"
                raise RequestRefusedError(refusal_code)
            if path.stat().st_size != item.bytes:
                refusal_code = "FILE_SIZE_DIFFERS"
                raise RequestRefusedError(refusal_code)
        except OSError:
            refusal_code = "LOCAL_FILE_MISSING"
            raise RequestRefusedError(refusal_code) from None
    copied = copy_small_files(request.files)
    canonical = dict(message)
    return BundleHandOver(
        handover_id=handover_identifier("dataset_bundle", canonical),
        repository=request.repository,
        remote_prefix=request.remote_prefix,
        request=dict(canonical.items()),
        copied=copied,
    )


@dataclass(frozen=True)
class ReleaseHandOver:
    """A checked release change: its hand-over identifier, change and queued message."""

    handover_id: str
    #: The validated change, and its canonical message, which is what is queued.
    change: ReleaseChange
    request: dict[str, Any]

    @property
    def repository(self) -> str:
        """The dataset repository the change applies to."""
        return self.change.repository


def _file_entries(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = message.get("files")
    if not isinstance(entries, list) or any(
        not isinstance(entry, Mapping) for entry in entries
    ):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code)
    return entries


def release_change_from_message(message: Mapping[str, Any]) -> ReleaseChange:
    """Return the typed change a message describes; ``RequestRefusedError`` if invalid.

    Only the message's shape is checked here, so the worker can rebuild the
    typed change from the queued message.
    """
    require_supported_version(message)
    command = message.get("command")
    try:
        if command == "publish_release":
            entries = _file_entries(message)
            return PublishRelease(
                repository=message.get("repository"),
                release=message.get("release"),
                build=message.get("build"),
                release_role=message.get("release_role"),
                make_current=message.get("make_current"),
                request_id=message.get("request_id"),
                files=tuple(
                    ReleaseFile(
                        name=entry.get("name"),
                        role=entry.get("role"),
                        local_path=Path(str(entry.get("local_path"))),
                        sha256=entry.get("sha256"),
                        bytes=entry.get("bytes"),
                    )
                    for entry in entries
                ),
            )
        if command in {"promote_build", "withdraw_build"}:
            change = PromoteBuild if command == "promote_build" else WithdrawBuild
            return change(
                repository=message.get("repository"),
                release=message.get("release"),
                build=message.get("build"),
                request_id=message.get("request_id"),
            )
    except RequestRefusedError:
        raise
    except (ValueError, TypeError):
        refusal_code = "INVALID_REQUEST"
        raise RequestRefusedError(refusal_code) from None
    refusal_code = "INVALID_REQUEST"
    raise RequestRefusedError(refusal_code)


def parse_release_change(message: Mapping[str, Any]) -> ReleaseHandOver:
    """Check a publication, promotion or withdrawal with the interface's rules.

    The queued request is the validated change's own message, so keys the
    rules do not know never reach the queue or the hand-over identifier.
    """
    change = release_change_from_message(message)
    if isinstance(change, PublishRelease):
        for item in change.files:
            if not item.local_path.is_absolute():
                refusal_code = "INVALID_REQUEST"
                raise RequestRefusedError(refusal_code)
            try:
                if not item.local_path.is_file():
                    refusal_code = "LOCAL_FILE_MISSING"
                    raise RequestRefusedError(refusal_code)
                if item.local_path.stat().st_size != item.bytes:
                    refusal_code = "FILE_SIZE_DIFFERS"
                    raise RequestRefusedError(refusal_code)
            except OSError:
                refusal_code = "LOCAL_FILE_MISSING"
                raise RequestRefusedError(refusal_code) from None
    canonical = change.message()
    return ReleaseHandOver(
        handover_id=handover_identifier(RELEASE_CHANGE, canonical),
        change=change,
        request=canonical,
    )

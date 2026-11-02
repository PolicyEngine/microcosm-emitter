"""Typed staged-dataset-bundle hand-overs, with each file's role named.

The build verifies its bundle against its manifest and writes the sidecars
(``staged_manifest.json``, ``sha256sums.txt``) as before, then hands the files
over by path with their roles and digests. The emitter writes them under
``<prefix>/<run_id>/`` in one commit, and is idempotent on the outputs'
digests: a repository copy of the bundle inventory (role ``staged_manifest``)
whose ``files`` equal the outputs' digests is ``already_staged``, and a
different one is refused as ``REMOTE_DIFFERS``.

Before answering, the emitter copies the bundle's small files into its queue,
smallest first, each of at most 32 MiB and together at most 64 MiB, after
checking that each holds exactly its declared size and SHA-256
(``FILE_SIZE_DIFFERS`` or ``FILE_DIGEST_DIFFERS`` otherwise). The receipt's
``copied_files`` names them. Once the call has returned, the build may
rewrite a copied file: the emitter uploads the bytes it copied. Every other
file is uploaded from its path, re-hashed first, and must stay unchanged
until the hand-over settles (``PRESERVED_BYTES_CHANGED`` otherwise).

Constructing a request that breaks the rules raises ``ValueError``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, NotRequired, TypedDict

from microcosm_emitter_interface.identifiers import (
    bare_file_name,
    repository_identifier,
    repository_prefix,
    role_identifier,
    safe_identifier,
)
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.submissions import CollectorProvenance

DEFAULT_BUNDLE_PREFIX: Final = "staged"
#: The bundle's own inventory, compared with the repository copy.
BUNDLE_INVENTORY_ROLE: Final = "staged_manifest"
#: The build's manifest; it gains evidence after staging, so it is not an output.
BUILD_MANIFEST_ROLE: Final = "build_manifest"
#: The ``sha256sum`` listing written beside the bundle.
CHECKSUMS_ROLE: Final = "checksums"
SIDECAR_ROLES: Final = frozenset(
    {BUNDLE_INVENTORY_ROLE, BUILD_MANIFEST_ROLE, CHECKSUMS_ROLE}
)
#: Roles of outputs readers look for by role rather than by file name.
DATASET_ROLE: Final = "dataset"
CALIBRATION_DIAGNOSTICS_ROLE: Final = "calibration_diagnostics"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BundleFile:
    """One preserved file and what it is.

    Every role other than the sidecar roles (``staged_manifest``,
    ``build_manifest``, ``checksums``) marks an output, for example
    ``dataset`` or ``calibration_diagnostics``.
    """

    name: str
    role: str
    local_path: Path
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        bare_file_name(self.name, label="bundle file name")
        role_identifier(self.role)
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            error_message = "sha256 must be a lowercase hexadecimal digest."
            raise ValueError(error_message)
        if (
            isinstance(self.bytes, bool)
            or not isinstance(self.bytes, int)
            or self.bytes < 0
        ):
            error_message = "bytes must be a nonnegative integer."
            raise ValueError(error_message)


def check_bundle_files(files: Sequence[BundleFile]) -> None:
    names = [item.name for item in files]
    if len(set(names)) != len(names):
        error_message = "A bundle names each file once."
        raise ValueError(error_message)
    roles = [item.role for item in files]
    if roles.count(BUNDLE_INVENTORY_ROLE) != 1:
        error_message = "A bundle has exactly one staged_manifest file."
        raise ValueError(error_message)
    for role in (BUILD_MANIFEST_ROLE, CHECKSUMS_ROLE):
        if roles.count(role) > 1:
            error_message = f"A bundle has at most one {role} file."
            raise ValueError(error_message)
    if all(role in SIDECAR_ROLES for role in roles):
        error_message = "A bundle has at least one output file."
        raise ValueError(error_message)


@dataclass(frozen=True)
class StageDatasetBundle:
    """Hand over a finished build's bundle for ``<prefix>/<run_id>/``.

    The files are written in the order given, in one commit whose message
    defaults to Microcosm's ``Stage <run_id> under <prefix>/<run_id>``.

    With ``collector``, the emitter also submits the calibration diagnostics
    and Orrery inputs the bundle names to the collector, once the Hugging
    Face commit lands and while its collector submissions are switched on.
    """

    repository: str
    run_id: str
    files: Sequence[BundleFile]
    prefix: str = DEFAULT_BUNDLE_PREFIX
    commit_message: str | None = None
    collector: CollectorProvenance | None = None
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        repository_identifier(self.repository)
        safe_identifier(self.run_id, label="run_id")
        repository_prefix(self.prefix)
        object.__setattr__(self, "files", tuple(self.files))
        if any(not isinstance(item, BundleFile) for item in self.files):
            error_message = "files must be BundleFile values."
            raise ValueError(error_message)
        check_bundle_files(self.files)
        if self.commit_message is not None and (
            not isinstance(self.commit_message, str) or not self.commit_message.strip()
        ):
            error_message = "commit_message must be nonempty when given."
            raise ValueError(error_message)
        if self.collector is not None and not isinstance(
            self.collector, CollectorProvenance
        ):
            error_message = "collector must be a CollectorProvenance value."
            raise ValueError(error_message)

    @property
    def remote_prefix(self) -> str:
        return f"{self.prefix}/{self.run_id}"

    def message(self) -> dict[str, Any]:
        message = {
            "protocol_version": self.protocol_version,
            "command": "stage_dataset_bundle",
            "repository": self.repository,
            "run_id": self.run_id,
            "prefix": self.prefix,
            "files": [
                {
                    "name": item.name,
                    "role": item.role,
                    "local_path": str(Path(item.local_path).resolve()),
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                }
                for item in self.files
            ],
            "commit_message": self.commit_message,
        }
        # Only when given, so a bundle without it keeps its message and
        # hand-over ID.
        if self.collector is not None:
            message["collector"] = self.collector.message()
        return message

    def handover_id(self) -> str:
        return handover_identifier("dataset_bundle", self.message())


def handover_identifier(kind: str, message: dict[str, Any]) -> str:
    """A stable identifier of a hand-over's content, shared with the emitter."""
    content = {
        key: value for key, value in message.items() if key != "protocol_version"
    }
    canonical = json.dumps(
        {"kind": kind, "request": content},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class DatasetBundleReceipt(TypedDict):
    """The emitter's latest result for one bundle hand-over.

    ``uploaded``, ``already_staged`` and ``failed`` are final. While
    ``pending``, ``error_code`` holds the last attempt's failure:
    ``REPOSITORY_UNAVAILABLE``, ``UPLOAD_FAILED``, ``CREDENTIAL_MISSING`` or
    ``PRESERVED_FILES_MISSING``. A final ``failed`` carries ``REMOTE_DIFFERS``
    or ``PRESERVED_BYTES_CHANGED``. ``refused`` requests were never queued.
    """

    handover_id: str
    status: Literal[
        "pending", "uploaded", "already_staged", "failed", "refused", "unavailable"
    ]
    error_code: str | None
    revision: str | None
    attempts: int
    repository: str
    prefix: str
    #: The files the emitter copied at hand-over; the build may rewrite them.
    copied_files: NotRequired[list[str]]


class WriteAccess(TypedDict):
    """Whether a remote stage could work, checked before a long build.

    ``credential`` is ``missing`` without ``HF_TOKEN``, ``HUGGINGFACE_TOKEN``
    or a saved login. ``can_write`` is ``None`` when the Hub does not describe
    the token's scope; then only the upload itself proves it.
    """

    status: Literal["checked", "unavailable"]
    credential: Literal["present", "missing"] | None
    reachable: bool | None
    can_write: bool | None
    error_type: str | None


def unavailable_bundle_receipt(request: StageDatasetBundle) -> DatasetBundleReceipt:
    """No emitter accepted the bundle; built without converting its files.

    ``handover_id`` is empty when the request's files cannot be converted.
    """
    try:
        handover_id = request.handover_id()
    except Exception:
        handover_id = ""
    return {
        "handover_id": handover_id,
        "status": "unavailable",
        "error_code": None,
        "revision": None,
        "attempts": 0,
        "repository": request.repository,
        "prefix": request.remote_prefix,
        "copied_files": [],
    }


def unavailable_write_access() -> WriteAccess:
    return {
        "status": "unavailable",
        "credential": None,
        "reachable": None,
        "can_write": None,
        "error_type": None,
    }

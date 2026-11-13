"""Typed release publication: one build folder and one ``releases.json`` change.

``PublishRelease`` names the release, the build (the attempt ID Microcosm
already assigns), whether to make the build current, and each file with its
role. The emitter writes the files to ``releases/<release>/<build>/`` and the
updated ``releases.json`` in one commit, after the checks in
``microcosm_emitter_interface.releases``. With ``CollectorProvenance``, the
emitter also submits the build's calibration diagnostics and Orrery inputs to
the collector after that commit. ``PromoteBuild`` and ``WithdrawBuild``
change only ``releases.json``; with ``CollectorProvenance``, the emitter
submits that commit's ``releases.json`` to the collector, naming the build
that is current after the change.

Publication is not best-effort: the receipt says whether the change was
``published``, is still ``pending`` (retried from the emitter's queue) or was
``refused`` by a rule. Constructing a request that breaks the shape rules
raises ``ValueError``.

Every request carries a ``request_id``, a new random one unless the caller
gives one. The emitter applies each request once: sending the same request
again (for example after its receipt was lost) returns that request's
receipt, while a new request is applied even if it repeats an earlier change,
such as promoting a build back after another was promoted, or retrying a
withdrawal or a refused change once its cause is gone.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict

from microcosm_emitter_interface.bundles import handover_identifier
from microcosm_emitter_interface.identifiers import (
    bare_file_name,
    repository_identifier,
    role_identifier,
    safe_identifier,
)
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.releases import (
    RELEASE_MANIFEST_NAME,
    is_pointer_file,
)
from microcosm_emitter_interface.submissions import CollectorProvenance

if TYPE_CHECKING:
    from collections.abc import Sequence

RELEASE_MANIFEST_ROLE: Final = "release_manifest"
RELEASE_CHANGE: Final = "release_change"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseFile:
    """One preserved file of the build and what it is (``dataset``, ...)."""

    name: str
    role: str
    local_path: Path
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        """Raise ``ValueError`` for an invalid name, role, digest or size.

        The name may also not be a ``latest*.json`` pointer file.
        """
        bare_file_name(self.name, label="release file name")
        role_identifier(self.role)
        if is_pointer_file(self.name):
            error_message = "A release file may not be a latest*.json pointer."
            raise ValueError(error_message)
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


def new_request_id() -> str:
    """Return a new random identifier for one release change request."""
    return uuid.uuid4().hex


def _check_reference(
    repository: str, release: str, build: str, request_id: str
) -> None:
    repository_identifier(repository)
    safe_identifier(release, label="release")
    safe_identifier(build, label="build")
    safe_identifier(request_id, label="request_id")


def _check_collector(
    collector: CollectorProvenance | None,
    *,
    release: str,
    build: str,
    change_name: str,
) -> None:
    """Raise ``ValueError`` unless ``collector`` is ``None`` or names this change.

    A given ``collector`` must be a ``CollectorProvenance`` naming the change's
    own release and build; ``change_name`` says which change it is in the
    message.
    """
    if collector is not None and not isinstance(collector, CollectorProvenance):
        error_message = "collector must be a CollectorProvenance value."
        raise ValueError(error_message)
    if collector is not None and (
        collector.release != release or collector.build_id != build
    ):
        error_message = (
            f"collector must name the {change_name}'s own release and build."
        )
        raise ValueError(error_message)


def _with_collector(
    message: dict[str, Any], collector: CollectorProvenance | None
) -> dict[str, Any]:
    """Add the ``collector`` key to ``message`` only when ``collector`` is given.

    Only when given, so a change without it keeps its message and hand-over ID.
    """
    if collector is not None:
        message["collector"] = collector.message()
    return message


@dataclass(frozen=True)
class PublishRelease:
    """Publish a new build of a release, optionally as its current build.

    ``release_role`` is the release's role in ``releases.json`` (for example
    ``national_default``); a release keeps the role it was first published
    with. The files must include ``release_manifest.json`` with the role
    ``release_manifest``, naming this release and build.

    With ``collector``, the emitter also submits the calibration diagnostics
    and Orrery inputs the files name to the collector, once the release
    commit lands and while its collector submissions are switched on
    (``HuggingFaceSettings.collector_submissions``). The submissions name the
    build's folder ``releases/<release>/<build>`` and the commit that added
    it. The provenance must name this publication's release and build, since
    the collector refuses a build folder whose provenance names another. The
    emitter copies the submitted files at hand-over, each of at most 32 MiB
    and together at most 64 MiB, smallest first, so the build may remove or
    rewrite those once the release has landed; a submitted file left out by
    those limits is read from its path and must stay unchanged until the
    hand-over settles. Handing an already published build over again with
    ``collector`` waits until the Hub reports the commit that added its
    folder, and is ``refused`` with ``REQUEST_EXPIRED`` if that takes an
    hour, even with submissions off.
    """

    repository: str
    release: str
    build: str
    release_role: str
    make_current: bool
    files: Sequence[ReleaseFile]
    request_id: str = field(default_factory=new_request_id)
    collector: CollectorProvenance | None = None
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        """Store ``files`` as a tuple; raise ``ValueError`` if a field is invalid.

        The repository, release, build, request ID and role must follow the
        identifier rules, ``make_current`` must be a bool, the files must
        be ``ReleaseFile`` values with distinct names, among them
        ``release_manifest.json``, which alone has the role
        ``release_manifest``, and a given ``collector`` must be a
        ``CollectorProvenance`` naming this release and build.
        """
        _check_reference(self.repository, self.release, self.build, self.request_id)
        role_identifier(self.release_role, label="release_role")
        if not isinstance(self.make_current, bool):
            error_message = "make_current must be a boolean."
            # The published constructors raise ValueError for every invalid value.
            raise ValueError(error_message)  # noqa: TRY004
        object.__setattr__(self, "files", tuple(self.files))
        if any(not isinstance(item, ReleaseFile) for item in self.files):
            error_message = "files must be ReleaseFile values."
            raise ValueError(error_message)
        names = [item.name for item in self.files]
        if len(set(names)) != len(names):
            error_message = "A build names each file once."
            raise ValueError(error_message)
        manifests = [item for item in self.files if item.name == RELEASE_MANIFEST_NAME]
        if len(manifests) != 1 or manifests[0].role != RELEASE_MANIFEST_ROLE:
            error_message = (
                f"A build has one {RELEASE_MANIFEST_NAME} with the role "
                f"{RELEASE_MANIFEST_ROLE}."
            )
            raise ValueError(error_message)
        if [item.role for item in self.files].count(RELEASE_MANIFEST_ROLE) != 1:
            error_message = f"Only {RELEASE_MANIFEST_NAME} has its role."
            raise ValueError(error_message)
        _check_collector(
            self.collector,
            release=self.release,
            build=self.build,
            change_name="publication",
        )

    def message(self) -> dict[str, Any]:
        """Return the ``publish_release`` request, with absolute local paths.

        The ``collector`` key is present only when ``collector`` is given.
        """
        message: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "command": "publish_release",
            "request_id": self.request_id,
            "repository": self.repository,
            "release": self.release,
            "build": self.build,
            "release_role": self.release_role,
            "make_current": self.make_current,
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
        }
        return _with_collector(message, self.collector)

    def handover_id(self) -> str:
        """Return the hand-over identifier the emitter also computes for it."""
        return handover_identifier(RELEASE_CHANGE, self.message())


@dataclass(frozen=True)
class PromoteBuild:
    """Make a listed, non-withdrawn build the release's current build.

    With ``collector``, which must name this promotion's release and build,
    the emitter also submits the ``releases.json`` of the promotion's commit
    to the collector as a ``releases-record`` submission, once that commit
    lands and while its collector submissions are switched on
    (``HuggingFaceSettings.collector_submissions``). The submission names the
    repository, that commit, and this build as the release's current build,
    with the provenance's country and ``licensed_data``; the collector then
    moves the country's latest calibration tree to follow ``releases.json``.
    When the build is already current, nothing is written, and the receipt
    and the submission name the commit that last changed ``releases.json``,
    never a later head; the promotion stays pending until the Hub reports
    that commit, even with submissions off, and is ``refused`` with
    ``REQUEST_EXPIRED`` if that takes an hour. A promotion that is refused
    sends nothing. A hand-over that
    landed while the emitter answering it had submissions off is not
    submitted when handed over again with submissions on
    (``RELEASES_RECORD_MISSING``); request it again with a new
    ``request_id``.
    """

    repository: str
    release: str
    build: str
    request_id: str = field(default_factory=new_request_id)
    collector: CollectorProvenance | None = None
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        """Raise ``ValueError`` if an identifier or ``collector`` is invalid.

        The identifiers must follow the shared identifier rules, and a given
        ``collector`` must be a ``CollectorProvenance`` naming this release
        and build.
        """
        _check_reference(self.repository, self.release, self.build, self.request_id)
        _check_collector(
            self.collector,
            release=self.release,
            build=self.build,
            change_name="promotion",
        )

    def message(self) -> dict[str, Any]:
        """Return the ``promote_build`` request.

        The ``collector`` key is present only when ``collector`` is given.
        """
        return _with_collector(
            {
                "protocol_version": self.protocol_version,
                "command": "promote_build",
                "request_id": self.request_id,
                "repository": self.repository,
                "release": self.release,
                "build": self.build,
            },
            self.collector,
        )

    def handover_id(self) -> str:
        """Return the hand-over identifier the emitter also computes for it."""
        return handover_identifier(RELEASE_CHANGE, self.message())


@dataclass(frozen=True)
class WithdrawBuild:
    """Mark a build withdrawn; its folder and files stay as published.

    With ``collector``, which must name this withdrawal's release and build,
    the emitter also submits the ``releases.json`` of the withdrawal's commit
    to the collector as a ``releases-record`` submission, as for
    ``PromoteBuild``. A withdrawal never changes ``current`` (the current
    build cannot be withdrawn), so the submission names the release's
    current build after the change, not the withdrawn one, with the
    provenance's country and ``licensed_data``. When the release has no
    current build, nothing is sent. Withdrawing a withdrawn build again
    writes nothing, and names the commit that last changed ``releases.json``,
    waiting for the Hub to report it as a promotion of the current build
    does.
    """

    repository: str
    release: str
    build: str
    request_id: str = field(default_factory=new_request_id)
    collector: CollectorProvenance | None = None
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        """Raise ``ValueError`` if an identifier or ``collector`` is invalid.

        The identifiers must follow the shared identifier rules, and a given
        ``collector`` must be a ``CollectorProvenance`` naming this release
        and build.
        """
        _check_reference(self.repository, self.release, self.build, self.request_id)
        _check_collector(
            self.collector,
            release=self.release,
            build=self.build,
            change_name="withdrawal",
        )

    def message(self) -> dict[str, Any]:
        """Return the ``withdraw_build`` request.

        The ``collector`` key is present only when ``collector`` is given.
        """
        return _with_collector(
            {
                "protocol_version": self.protocol_version,
                "command": "withdraw_build",
                "request_id": self.request_id,
                "repository": self.repository,
                "release": self.release,
                "build": self.build,
            },
            self.collector,
        )

    def handover_id(self) -> str:
        """Return the hand-over identifier the emitter also computes for it."""
        return handover_identifier(RELEASE_CHANGE, self.message())


type ReleaseChange = PublishRelease | PromoteBuild | WithdrawBuild


class ReleaseReceipt(TypedDict):
    """The emitter's latest result for one release change.

    ``published`` and ``refused`` are final; ``pending`` is retried, with the
    last attempt's ``error_code`` (``REPOSITORY_UNAVAILABLE``,
    ``UPLOAD_FAILED``, ``PARENT_CONFLICT``, ``CREDENTIAL_MISSING``,
    ``PRESERVED_FILES_MISSING``). A refusal's ``error_code`` names the broken
    rule, for example ``BUILD_FOLDER_EXISTS`` or ``POINTER_FILES_PRESENT``;
    ``REQUEST_EXPIRED`` when the change was still pending an hour after it was
    handed over; ``NEWER_CHANGE_LANDED`` when a promotion was overtaken: a
    change to ``current`` of the same release (a promotion, or a publication
    that makes its build current), requested after it, landed first. Request
    such a change again, as a new request, if it is still wanted. A
    withdrawal never changes ``current``, so it is never refused for order.
    A publication that makes its build current is never refused for being
    overtaken: it is ``published`` with its build listed, ``current`` left as
    the newer change set it, and a ``message`` that says so. Another
    machine's newer change that left ``releases.json`` unchanged is not
    seen; only the hour's expiry bounds how late an older change to that
    release can land after it.

    ``current`` says which build is current. On a ``published`` receipt it is
    the release's current build in ``releases.json`` once this change was
    applied (or, when nothing needed writing, as the writer found it), and
    ``None`` when the release has no current build. On every other status it
    is ``None``. A publication with ``make_current=True`` therefore made its
    build current exactly when ``status == "published"`` and ``current ==
    build``; it can be ``published`` without that when a change to
    ``current`` requested after it landed first, or when the build was
    already published, and ``message`` then says which.
    """

    handover_id: str
    status: Literal["pending", "published", "refused", "unavailable"]
    error_code: str | None
    message: str | None
    commit: str | None
    repository: str
    release: str
    build: str
    attempts: int
    current: str | None


def unavailable_release_receipt(request: ReleaseChange) -> ReleaseReceipt:
    """No emitter accepted the change; built without converting its files.

    ``handover_id`` is empty when the request's files cannot be converted.
    """
    try:
        handover_id = request.handover_id()
    # No call raises into the build: a request whose files cannot be converted
    # gets an empty handover_id.
    except Exception:  # noqa: BLE001
        handover_id = ""
    return {
        "handover_id": handover_id,
        "status": "unavailable",
        "error_code": None,
        "message": None,
        "commit": None,
        "repository": request.repository,
        "release": request.release,
        "build": request.build,
        "attempts": 0,
        "current": None,
    }

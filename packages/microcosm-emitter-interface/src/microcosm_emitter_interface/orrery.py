"""Typed Orrery input hand-over: the emitter's existing graph publication queue.

The emitter hashes the selected preserved files, durably queues them and
delivers them; this package only names the files and their roles. The shapes
equal ``microcosm_emitter.orrery.contracts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

type PublicationFileRole = Literal[
    "graph",
    "schema",
    "index",
    "declaration",
    "manifest",
    "binding",
    "summaries",
    "upstream",
]


class PublicationFile(TypedDict):
    """One preserved file, identified by its exact bytes and allowed role."""

    name: str
    role: PublicationFileRole
    bytes: int
    sha256: str


class PublicationInventory(TypedDict):
    """The versioned file inventory the emitter computed and will deliver."""

    version: Literal[1]
    publication_id: str
    files: list[PublicationFile]


class PublicationReceipt(TypedDict):
    """The emitter's latest delivery result for one publication."""

    version: Literal[1]
    publication_id: str | None
    status: Literal["pending", "published", "skipped", "failed", "no_evidence"]
    error_code: str | None
    url: NotRequired[str | None]
    attempts: NotRequired[int]
    inventory_sha256: NotRequired[str]
    preserved_directory: NotRequired[str]
    recorded_execution: NotRequired[bool]


@dataclass(frozen=True)
class OrrerySettings:
    """Configuration for the emitter's Orrery module; defaults are production."""

    development_collector_url: str | None = None
    publication_origin: str | None = None

    def configuration(self, spool_path: Path) -> dict[str, Any]:
        """Return the Orrery module's startup settings for the queue at ``spool_path``.

        ``development_collector_url`` and ``publication_origin`` are included
        only when set.
        """
        configuration: dict[str, Any] = {"spool_path": str(spool_path)}
        if self.development_collector_url is not None:
            configuration["development_collector_url"] = self.development_collector_url
        if self.publication_origin is not None:
            configuration["publication_origin"] = self.publication_origin
        return configuration


def unavailable_receipt(
    publication_id: str | None, *, error_code: str = "local_service_unavailable"
) -> PublicationReceipt:
    """No acknowledgement: no claim that the emitter queued anything.

    ``error_code`` is ``local_service_unavailable`` when no emitter answered,
    or ``invalid_arguments`` when the call's arguments could not be sent.
    """
    return {
        "version": 1,
        "publication_id": publication_id,
        "status": "failed",
        "error_code": error_code,
    }


# The Orrery module's requests. Both this package's client and the emitter's
# in-process client build them here, inside their error handling, so the two
# send the same messages and neither raises on an unconvertible argument.


def inventory_message(
    directory: str | os.PathLike[str],
    roles: Mapping[str, PublicationFileRole],
    publication_id: str | None,
) -> dict[str, Any]:
    """Build the request to inventory the files ``roles`` names in ``directory``.

    ``roles`` maps each file name to its role, and ``directory`` is sent as
    an absolute path.
    """
    return {
        "command": "inventory",
        "directory": str(Path(directory).resolve()),
        "roles": dict(roles),
        "publication_id": publication_id,
    }


def enqueue_message(
    directory: str | os.PathLike[str], inventory: PublicationInventory
) -> dict[str, Any]:
    """Build the request to queue an inventory's files from ``directory``."""
    return {
        "command": "enqueue",
        "directory": str(Path(directory).resolve()),
        "inventory": inventory,
    }


def receipt_message(publication_id: str) -> dict[str, Any]:
    """Build the request for one publication's latest receipt."""
    return {"command": "receipt", "publication_id": publication_id}


def retry_message(publication_id: str) -> dict[str, Any]:
    """Build the request to attempt a pending publication's delivery again now."""
    return {"command": "retry", "publication_id": publication_id}


def publication_id_of(inventory: Any) -> str | None:
    """Return the inventory's ``publication_id``, or None if it has no string one."""
    try:
        value = inventory["publication_id"]
    except Exception:
        return None
    return value if isinstance(value, str) else None


def answered_inventory(answer: Mapping[str, Any] | None) -> PublicationInventory | None:
    """Return the inventory in the module's answer, or None if it holds none."""
    value = None if answer is None else answer.get("inventory")
    return cast("PublicationInventory", value) if isinstance(value, dict) else None


def answered_receipt(
    answer: Mapping[str, Any] | None,
    publication_id: str | None,
    *,
    error_code: str = "local_service_unavailable",
) -> PublicationReceipt:
    """Return the receipt in the module's answer, or an unavailable receipt.

    An answer whose receipt has no status is not a receipt: the build reads
    ``status`` from every receipt it gets.
    """
    value = None if answer is None else answer.get("receipt")
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return cast("PublicationReceipt", value)
    return unavailable_receipt(publication_id, error_code=error_code)

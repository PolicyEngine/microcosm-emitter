"""Explicit data contracts for immutable graph publication."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

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
    """One public file, identified by its exact bytes and allowed role."""

    name: str
    role: PublicationFileRole
    bytes: int
    sha256: str


class PublicationInventory(TypedDict):
    """The versioned file inventory submitted to the publication API."""

    version: Literal[1]
    publication_id: str
    files: list[PublicationFile]


class PublicationReceipt(TypedDict):
    """Local publication result, including retry and build-snapshot fields."""

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
class ClaimedGraphPublication:
    """A job reserved until lease_until; the timestamp also identifies its lease."""

    publication_id: str
    directory: Path
    inventory: PublicationInventory
    lease_until: float

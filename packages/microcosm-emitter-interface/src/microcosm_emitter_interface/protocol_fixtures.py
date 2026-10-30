"""Recorded protocol messages, shipped so every emitter release can test them.

``fixtures/protocol-v<N>/`` holds one JSON file per message this package sends
under protocol version ``N``. The interface's tests check that each typed call
sends exactly its fixture; the emitter's contract tests check that it accepts
every fixture of every version it supports. A released fixture never changes:
a new message shape gets a new fixture, and an incompatible one a new version.

Each file holds:

- ``protocol_version`` and ``module`` (``telemetry``, ``orrery`` or ``host``);
- ``kind``:
  - ``message`` (acknowledged) or ``query`` (answered): a module's message;
  - ``configuration``: a module's startup settings;
  - ``hello``: the request for the host's description;
  - ``launch``: how the command is started, as ``arguments`` after the command
    and the ``standard_input`` object (module name to its configuration);
  - ``envelope``: one whole socket request, as the host receives it;
- ``sequence``: the order within a module, for requests that build on earlier
  ones;
- ``body``: the message, with the placeholders ``{directory}``,
  ``{spool_path}``, ``{socket_path}`` and ``{parent_pid}`` in string values;
- ``files``: text files to create in ``{directory}`` before sending;
- ``response_keys``: the keys an answer must contain (at least);
- ``response``: for an envelope, ``acknowledgement`` (``ok``) or ``answer``
  (a JSON object).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable
    from pathlib import Path

FIXTURE_ROOT = "fixtures"
DIRECTORY_PLACEHOLDER = "{directory}"
SPOOL_PATH_PLACEHOLDER = "{spool_path}"
SOCKET_PATH_PLACEHOLDER = "{socket_path}"
PARENT_PID_PLACEHOLDER = "{parent_pid}"

type FixtureKind = Literal[
    "message", "query", "configuration", "hello", "launch", "envelope"
]
type EnvelopeResponse = Literal["acknowledgement", "answer"]


@dataclass(frozen=True)
class ProtocolFixture:
    """One recorded protocol message, read from a file under ``fixtures/``.

    ``name`` is the file name without ``.json`` and ``path`` its path below
    ``fixtures/``; the other fields hold the file's keys of the same names.
    """

    name: str
    path: str
    protocol_version: int
    module: str
    kind: FixtureKind
    body: dict[str, Any]
    sequence: int = 0
    files: dict[str, str] = field(default_factory=dict)
    response_keys: tuple[str, ...] = ()
    response: EnvelopeResponse | None = None


def _fixture_files() -> list[tuple[str, Traversable]]:
    root = files("microcosm_emitter_interface").joinpath(FIXTURE_ROOT)
    found = []
    for version_directory in root.iterdir():
        if not version_directory.is_dir() or not version_directory.name.startswith(
            "protocol-v"
        ):
            continue
        for entry in version_directory.iterdir():
            if entry.name.endswith(".json"):
                found.append((f"{version_directory.name}/{entry.name}", entry))
    return sorted(found, key=lambda item: item[0])


def protocol_fixtures(version: int | None = None) -> list[ProtocolFixture]:
    """Every shipped fixture, or those of one protocol version."""
    fixtures = []
    for relative_path, entry in _fixture_files():
        document = json.loads(entry.read_text(encoding="utf-8"))
        fixture = ProtocolFixture(
            name=entry.name.removesuffix(".json"),
            path=relative_path,
            protocol_version=document["protocol_version"],
            module=document["module"],
            kind=document["kind"],
            body=document["body"],
            sequence=document.get("sequence", 0),
            files=dict(document.get("files", {})),
            response_keys=tuple(document.get("response_keys", ())),
            response=document.get("response"),
        )
        if relative_path.split("/", 1)[0] != f"protocol-v{fixture.protocol_version}":
            error_message = f"{relative_path} is filed under the wrong version."
            raise ValueError(error_message)
        if version is None or fixture.protocol_version == version:
            fixtures.append(fixture)
    return fixtures


def fixture_digests() -> dict[str, str]:
    """SHA-256 of every fixture file, keyed by its path below ``fixtures/``."""
    return {
        relative_path: hashlib.sha256(entry.read_bytes()).hexdigest()
        for relative_path, entry in _fixture_files()
    }


def materialize(
    fixture: ProtocolFixture,
    *,
    directory: Path,
    spool_path: Path,
    socket_path: Path | None = None,
    parent_pid: int | None = None,
) -> dict[str, Any]:
    """Return the fixture's body with its placeholders replaced.

    Placeholders are replaced in string values at any depth;
    ``{socket_path}`` and ``{parent_pid}`` only when they are given.
    """
    replacements = {
        DIRECTORY_PLACEHOLDER: str(directory),
        SPOOL_PATH_PLACEHOLDER: str(spool_path),
    }
    if socket_path is not None:
        replacements[SOCKET_PATH_PLACEHOLDER] = str(socket_path)
    if parent_pid is not None:
        replacements[PARENT_PID_PLACEHOLDER] = str(parent_pid)

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            for placeholder, replacement in replacements.items():
                value = value.replace(placeholder, replacement)
            return value
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    return replace(fixture.body)

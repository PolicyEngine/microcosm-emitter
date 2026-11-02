"""Identifier and path rules shared by the interface and the emitter.

The interface refuses to build a request that breaks them, and the emitter
checks every request it receives with the same functions.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Final

SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REPOSITORY_IDENTIFIER: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
ROLE_IDENTIFIER: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Folders that only the release writer changes; staging never writes there.
RESERVED_PREFIXES: Final = frozenset({"releases"})


def safe_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
        error_message = f"{label} must match {SAFE_IDENTIFIER.pattern}."
        raise ValueError(error_message)
    return value


def repository_identifier(value: object) -> str:
    """A Hugging Face repository identifier, ``owner/name``."""
    if not isinstance(value, str) or not REPOSITORY_IDENTIFIER.fullmatch(value):
        error_message = "repository must be a Hugging Face owner/name identifier."
        raise ValueError(error_message)
    return value


def role_identifier(value: object, *, label: str = "role") -> str:
    if not isinstance(value, str) or not ROLE_IDENTIFIER.fullmatch(value):
        error_message = f"{label} must match {ROLE_IDENTIFIER.pattern}."
        raise ValueError(error_message)
    return value


def repository_prefix(value: object) -> str:
    """A folder of safe segments that is not reserved for release records."""
    if not isinstance(value, str) or not value:
        error_message = "prefix must be a nonempty relative folder."
        raise ValueError(error_message)
    segments = value.split("/")
    for segment in segments:
        safe_identifier(segment, label="prefix segment")
    if segments[0] in RESERVED_PREFIXES:
        error_message = f"prefix may not start with {segments[0]!r}."
        raise ValueError(error_message)
    return value


def relative_file_path(value: object, *, label: str) -> str:
    """A relative POSIX path that cannot leave its folder."""
    if not isinstance(value, str) or not value or "\\" in value:
        error_message = f"{label} must be a nonempty POSIX relative path."
        raise ValueError(error_message)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        error_message = f"{label} must stay inside its folder."
        raise ValueError(error_message)
    return value


def bare_file_name(value: object, *, label: str) -> str:
    """One path segment: a file name without folders."""
    name = relative_file_path(value, label=label)
    if "/" in name:
        error_message = f"{label} must be a bare file name."
        raise ValueError(error_message)
    return name

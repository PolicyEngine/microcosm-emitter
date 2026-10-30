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
    """Return ``value`` if it is a safe identifier; raise ``ValueError`` if not.

    A safe identifier is 1 to 128 letters, digits, ``.``, ``_`` or ``-``,
    starting with a letter or digit. The error message names ``label``.
    """
    if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
        error_message = f"{label} must match {SAFE_IDENTIFIER.pattern}."
        raise ValueError(error_message)
    return value


def repository_identifier(value: object) -> str:
    """Return ``value`` if it is a Hugging Face ``owner/name`` identifier.

    Raise ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not REPOSITORY_IDENTIFIER.fullmatch(value):
        error_message = "repository must be a Hugging Face owner/name identifier."
        raise ValueError(error_message)
    return value


def role_identifier(value: object, *, label: str = "role") -> str:
    """Return ``value`` if it is a role identifier; raise ``ValueError`` if not.

    A role identifier is 1 to 64 lowercase letters, digits or underscores,
    starting with a letter. The error message names ``label``.
    """
    if not isinstance(value, str) or not ROLE_IDENTIFIER.fullmatch(value):
        error_message = f"{label} must match {ROLE_IDENTIFIER.pattern}."
        raise ValueError(error_message)
    return value


def repository_prefix(value: object) -> str:
    """Return ``value`` if it is a folder of safe segments not reserved for releases.

    Each ``/``-separated segment must be a safe identifier, and the first may
    not be in ``RESERVED_PREFIXES`` (``releases``). Raise ``ValueError``
    otherwise.
    """
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
    """Return ``value`` if it is a relative POSIX path that cannot leave its folder.

    Raise ``ValueError`` for an empty or absolute path, a backslash, or an
    empty, ``.`` or ``..`` segment.
    """
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

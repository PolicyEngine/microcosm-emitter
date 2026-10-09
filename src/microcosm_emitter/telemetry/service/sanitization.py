"""Service-side size bounds and credential redaction before persistence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from microcosm_emitter.telemetry.protocol import (
    MAX_TELEMETRY_COLLECTION_ITEMS,
    MAX_TELEMETRY_DETAILS_BYTES,
    MAX_TELEMETRY_DETAILS_DEPTH,
    MAX_TELEMETRY_TEXT_CHARS,
)

_SENSITIVE_KEY_PARTS: Final = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "traceback",
)
_SECRET_TEXT: Final = re.compile(
    r"(?i)(?:bearer\s+[^\s]+|(?:token|secret|password|credential)\s*[:=]\s*[^\s]+|hf_[A-Za-z0-9_-]{8,})"
)
_REDACTED_VALUE: Final = "[redacted]"
_MAXIMUM_DEPTH_VALUE: Final = "[maximum depth]"
_DETAILS_TRUNCATED_KEY: Final = "telemetry_details_truncated"


def sanitize_text(value: str, *, limit: int = MAX_TELEMETRY_TEXT_CHARS) -> str:
    """Redact credential-like text and apply a character limit."""

    return _SECRET_TEXT.sub(_REDACTED_VALUE, value)[:limit]


def sanitize_json(value: Any, *, depth: int = 0) -> Any:
    """Return bounded JSON-compatible telemetry without model objects."""

    if depth >= MAX_TELEMETRY_DETAILS_DEPTH:
        return _MAXIMUM_DEPTH_VALUE
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:MAX_TELEMETRY_COLLECTION_ITEMS]:
            name = str(key)
            if any(part in name.lower() for part in _SENSITIVE_KEY_PARTS):
                result[name] = _REDACTED_VALUE
            else:
                result[name] = sanitize_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_json(item, depth=depth + 1)
            for item in value[:MAX_TELEMETRY_COLLECTION_ITEMS]
        ]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (int, bool)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return sanitize_json(item())
    return sanitize_text(str(value))


def sanitize_details(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted details object within the wire-size limit."""

    sanitized = sanitize_json(value)
    assert isinstance(sanitized, dict)
    if (
        len(json.dumps(sanitized, separators=(",", ":")).encode())
        <= MAX_TELEMETRY_DETAILS_BYTES
    ):
        return sanitized
    compact: dict[str, Any] = {_DETAILS_TRUNCATED_KEY: True}
    for key, item in sanitized.items():
        if not (isinstance(item, (str, int, float, bool)) or item is None):
            continue
        candidate = {**compact, key: item}
        if (
            len(json.dumps(candidate, separators=(",", ":")).encode())
            > MAX_TELEMETRY_DETAILS_BYTES
        ):
            break
        compact[key] = item
    return compact

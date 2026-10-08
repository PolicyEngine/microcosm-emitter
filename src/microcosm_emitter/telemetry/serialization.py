"""Convert Python request arguments to JSON without interpreting telemetry."""

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def wire_value(value: Any) -> Any:
    """Preserve values where possible, including scientific-library scalars."""
    if isinstance(value, Mapping):
        return {str(key): wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return wire_value(item())
    return str(value)

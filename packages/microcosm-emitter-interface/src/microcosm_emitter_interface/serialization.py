"""Convert Python call arguments to JSON without interpreting them."""

import math
from collections.abc import Mapping
from pathlib import Path

# A JSON value: null, a boolean, a number, a string, an array or an object.
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


def wire_value(value: object) -> JsonValue:
    """Preserve values where possible, including scientific-library scalars.

    The same conversion as ``microcosm_emitter.telemetry.serialization``, so
    the emitter receives the messages its own in-process client sends.
    """
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

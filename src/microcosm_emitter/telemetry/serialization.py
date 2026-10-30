"""Convert Python request arguments to JSON without interpreting telemetry.

The conversion is the interface package's, so the in-process client and
``microcosm_emitter_interface`` send identical messages.
"""

from microcosm_emitter_interface.serialization import wire_value

# Every public name, including those re-exported from the interface package,
# so type checkers of this py.typed package treat them as exported.
__all__ = [
    "wire_value",
]

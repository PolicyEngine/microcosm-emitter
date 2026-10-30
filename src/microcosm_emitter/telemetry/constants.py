"""Client configuration; no database or hosted-service imports."""

from typing import Final

from microcosm_emitter_interface.telemetry import (
    DEFAULT_HEARTBEAT_SECONDS,
)

# Every public name, including those re-exported from the interface package,
# so type checkers of this py.typed package treat them as exported.
__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "LOCAL_ONLY_REJECTED_EVENTS",
    "QUEUE_WARNING",
    "REJECTED_EVENTS_MESSAGE",
    "SERVICE_START_WARNING",
    "TELEMETRY_MODULE",
]

TELEMETRY_MODULE: Final = "telemetry"
SERVICE_START_WARNING: Final = "Local telemetry emitter service failed to start ({error_type}); the build will continue."
QUEUE_WARNING: Final = (
    "Telemetry could not be queued ({error_type}); the build will continue."
)
LOCAL_ONLY_REJECTED_EVENTS: Final = "collector_rejected_events"
REJECTED_EVENTS_MESSAGE: Final = (
    "Microcosm telemetry is local-only for this run: the collector rejected its "
    "events (HTTP {status}), so they will not be retried. The dataset build will "
    "continue."
)

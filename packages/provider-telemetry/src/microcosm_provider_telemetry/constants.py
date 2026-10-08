"""Client configuration; no database or hosted-service imports."""

from typing import Final

TELEMETRY_MODULE: Final = "telemetry"
DEFAULT_HEARTBEAT_SECONDS: Final = 60.0
SERVICE_START_WARNING: Final = "Local telemetry emitter service failed to start ({error_type}); the build will continue."
QUEUE_WARNING: Final = (
    "Telemetry could not be queued ({error_type}); the build will continue."
)

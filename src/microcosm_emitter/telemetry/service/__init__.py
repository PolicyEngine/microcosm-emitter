"""Explicitly loaded telemetry module for the local service host."""

from microcosm_emitter.host.contracts import (
    JsonObject,
    ModuleContext,
    ServiceModule,
)


def create_module(configuration: JsonObject, context: ModuleContext) -> ServiceModule:
    """Construct the telemetry module without starting background work."""
    from microcosm_emitter.telemetry.service.module import TelemetryModule

    return TelemetryModule(configuration, context)

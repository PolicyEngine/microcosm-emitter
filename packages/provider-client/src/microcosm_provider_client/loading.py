"""Load only the explicitly requested installed service module."""

from importlib.metadata import entry_points

from microcosm_provider_client.constants import MODULE_GROUP
from microcosm_provider_client.contracts import (
    JsonObject,
    ModuleContext,
    ServiceModule,
)
from microcosm_provider_client.errors import LocalServiceError


def load_module(
    name: str, configuration: JsonObject, context: ModuleContext
) -> ServiceModule:
    matches = list(entry_points(group=MODULE_GROUP, name=name))
    if len(matches) != 1:
        raise LocalServiceError(
            f"Expected exactly one installed module named {name!r}."
        )
    return matches[0].load()(configuration, context)

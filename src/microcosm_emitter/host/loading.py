"""Load only the explicitly requested installed service module."""

from importlib.metadata import entry_points

from microcosm_emitter.host.constants import MODULE_GROUP
from microcosm_emitter.host.contracts import (
    JsonObject,
    ModuleContext,
    ServiceModule,
)
from microcosm_emitter.host.errors import LocalServiceError, ModuleNotInstalledError


def load_module(
    name: str, configuration: JsonObject, context: ModuleContext
) -> ServiceModule:
    matches = list(entry_points(group=MODULE_GROUP, name=name))
    if not matches:
        error_message = f"No installed module is named {name!r}."
        raise ModuleNotInstalledError(error_message)
    if len(matches) != 1:
        error_message = f"Expected exactly one installed module named {name!r}."
        raise LocalServiceError(error_message)
    return matches[0].load()(configuration, context)

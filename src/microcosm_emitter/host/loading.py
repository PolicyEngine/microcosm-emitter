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
    """Load the installed module ``name`` and build it from its configuration.

    The module is the one entry point named ``name`` in the ``MODULE_GROUP``
    group, called with ``configuration`` and ``context``. Raises
    ``ModuleNotInstalledError`` when none is installed and ``LocalServiceError``
    when more than one is.
    """
    matches = list(entry_points(group=MODULE_GROUP, name=name))
    if not matches:
        error_message = f"No installed module is named {name!r}."
        raise ModuleNotInstalledError(error_message)
    if len(matches) != 1:
        error_message = f"Expected exactly one installed module named {name!r}."
        raise LocalServiceError(error_message)
    return matches[0].load()(configuration, context)

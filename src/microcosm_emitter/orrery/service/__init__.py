"""Explicit graph-publication service module registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from microcosm_emitter.orrery.service.module import OrreryModule

if TYPE_CHECKING:
    from microcosm_emitter.host.contracts import JsonObject, ModuleContext


def create_module(configuration: JsonObject, context: ModuleContext) -> OrreryModule:
    """Construct a module without starting workers or performing network I/O."""
    return OrreryModule(configuration, context)

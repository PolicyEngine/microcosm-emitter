"""Explicit graph-publication service module registration."""

from microcosm_provider_orrery.service.module import OrreryModule


def create_module(configuration, context):
    """Construct a module without starting workers or performing network I/O."""
    return OrreryModule(configuration, context)

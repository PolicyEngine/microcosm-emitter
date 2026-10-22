"""Choose the one collector origin that receives the builder's Hugging Face token.

Every service module that talks to the collector resolves its origin here, so
the modules sharing a collector session agree on it, and moving the collector
changes constants rather than modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from microcosm_emitter.core.auth import _collector_origin, _development_collector_url
from microcosm_emitter.core.constants import (
    CONFLICTING_COLLECTOR_ORIGIN_ERROR,
    PRODUCTION_COLLECTOR_URL,
    TRUSTED_COLLECTOR_ORIGINS,
    UNTRUSTED_COLLECTOR_ORIGIN_ERROR,
)

COLLECTOR_ORIGIN_SETTING = "collector_origin"
DEVELOPMENT_COLLECTOR_URL_SETTING = "development_collector_url"


def resolve_collector_origin(
    *,
    development_collector_url: str | None = None,
    collector_origin: str | None = None,
) -> str:
    """Return the collector origin selected by at most one of two settings.

    With neither, the package's production collector. ``development_collector_url``
    must be a loopback URL, for tests and local development.
    ``collector_origin`` must be one of ``TRUSTED_COLLECTOR_ORIGINS``: it can
    choose among the origins this release already trusts, never a new one.
    """

    if development_collector_url is not None and collector_origin is not None:
        raise ValueError(CONFLICTING_COLLECTOR_ORIGIN_ERROR)
    if development_collector_url is not None:
        return _development_collector_url(development_collector_url)
    if collector_origin is None:
        return _collector_origin(PRODUCTION_COLLECTOR_URL)
    origin = _collector_origin(collector_origin)
    if origin not in TRUSTED_COLLECTOR_ORIGINS:
        raise ValueError(UNTRUSTED_COLLECTOR_ORIGIN_ERROR)
    return origin


def configured_collector_origin(configuration: Mapping[str, Any]) -> str:
    """Return the origin a module's configuration selects.

    An empty or null setting counts as unset, as modules have always treated
    ``development_collector_url``.
    """

    return resolve_collector_origin(
        development_collector_url=(
            configuration.get(DEVELOPMENT_COLLECTOR_URL_SETTING) or None
        ),
        collector_origin=configuration.get(COLLECTOR_ORIGIN_SETTING) or None,
    )

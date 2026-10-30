"""Choose the one collector origin that receives the builder's Hugging Face token.

Every service module that talks to the collector resolves its origin here, so
the modules sharing a collector session agree on it, and moving the collector
changes constants rather than modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from microcosm_emitter.core.auth import _collector_origin, _development_collector_url
from microcosm_emitter.core.constants import (
    COLLECTOR_SETTING_TYPE_ERROR,
    CONFLICTING_COLLECTOR_ORIGIN_ERROR,
    PRODUCTION_COLLECTOR_ORIGIN_PAIR,
    PRODUCTION_COLLECTOR_URL,
    TRUSTED_COLLECTOR_ORIGINS,
    UNTRUSTED_COLLECTOR_ORIGIN_ERROR,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    A setting that is not a string raises ``ValueError``, like any other
    refused setting.
    """
    for setting in (development_collector_url, collector_origin):
        if setting is not None and not isinstance(setting, str):
            raise ValueError(COLLECTOR_SETTING_TYPE_ERROR)
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


def same_collector_origins(origin: str) -> frozenset[str]:
    """Return the origins of the collector that ``origin`` reaches, ``origin`` included.

    Only the two origins of ``PRODUCTION_COLLECTOR_ORIGIN_PAIR`` are one
    collector, so collector rows queued under either (such as rows that earlier
    releases queued under the Cloud Run address) are delivered through the one
    this emitter resolved. Any other origin, trusted or a development one, has
    only itself.
    """
    if origin in PRODUCTION_COLLECTOR_ORIGIN_PAIR:
        return PRODUCTION_COLLECTOR_ORIGIN_PAIR
    return frozenset({origin})


def configured_collector_origin(configuration: Mapping[str, Any]) -> str:
    """Return the origin a module's configuration selects.

    An empty or null setting counts as unset, as the Orrery and Hugging Face
    modules already did for ``development_collector_url``.
    """
    return resolve_collector_origin(
        development_collector_url=(
            configuration.get(DEVELOPMENT_COLLECTOR_URL_SETTING) or None
        ),
        collector_origin=configuration.get(COLLECTOR_ORIGIN_SETTING) or None,
    )

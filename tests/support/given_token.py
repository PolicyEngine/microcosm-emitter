"""The collector token the Modal runner gives an emitter, for tests.

The runner sets three variables from `microcosm-emitter telemetry-token`'s
output; ``given_token_variables`` builds them with a synthetic token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from microcosm_emitter.core.constants import (
    COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
    COLLECTOR_TOKEN_ORIGIN_VARIABLE,
    COLLECTOR_TOKEN_VARIABLE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

GIVEN_COLLECTOR_TOKEN = "synthetic-telemetry-token"
GIVEN_TOKEN_VARIABLES = (
    COLLECTOR_TOKEN_VARIABLE,
    COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
    COLLECTOR_TOKEN_ORIGIN_VARIABLE,
)


def given_token_variables(
    origin: str,
    *,
    expires_in: timedelta = timedelta(hours=18),
    token: str = GIVEN_COLLECTOR_TOKEN,
) -> dict[str, str]:
    """Return the three variables for a token obtained from ``origin``."""
    expires_at = datetime.now(UTC) + expires_in
    return {
        COLLECTOR_TOKEN_VARIABLE: token,
        COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE: expires_at.isoformat(timespec="seconds"),
        COLLECTOR_TOKEN_ORIGIN_VARIABLE: origin,
    }


def set_variables(monkeypatch, variables: Mapping[str, str]) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

"""The description of the machine a build runs on, without naming it."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def machine_environment(
    environ: Mapping[str, str] = os.environ,  # noqa: ARG001
    system_platform: str = sys.platform,  # noqa: ARG001
) -> dict[str, object]:
    """Not yet implemented."""
    return {}

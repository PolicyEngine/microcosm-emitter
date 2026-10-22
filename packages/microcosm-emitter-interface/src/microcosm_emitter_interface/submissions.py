"""What the emitter needs to submit handed-over files to the collector.

The collector's artifact intake (PolicyEngine/microcosm-collector,
``docs/artifact-submissions.md``) records which build produced each
submission. A bundle hand-over says so with ``CollectorProvenance``; without
it, the bundle goes only to Hugging Face. The emitter submits only while its
collector submissions are switched on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from microcosm_emitter_interface.identifiers import safe_identifier

#: Bundle roles of the Orrery inputs the collector projects from. The
#: collector's ``orrery-inputs`` kind names them ``schema``,
#: ``execution_overlay``, ``upstream`` and ``evidence``.
ORRERY_SCHEMA_ROLE: Final = "orrery_schema"
EXECUTION_OVERLAY_ROLE: Final = "execution_overlay"
ORRERY_UPSTREAM_ROLE: Final = "orrery_upstream"
ORRERY_EVIDENCE_ROLE: Final = "orrery_evidence"
_COUNTRY_CODE: Final = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True)
class CollectorProvenance:
    """The build behind a hand-over, as the collector records it.

    ``build_id`` is the attempt ID Microcosm assigns, the same from staging
    through release. ``licensed_data`` says whether the build used licensed
    microdata. The collector keeps the Orrery outputs of such a build private
    until output clearance; calibration data may be public whether or not
    the build used licensed data (decision D10); and native evidence is never
    published. ``release`` is ``None`` for a staged build that is not a
    release.
    """

    country_code: str
    build_id: str
    licensed_data: bool
    release: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.country_code, str) or not _COUNTRY_CODE.fullmatch(
            self.country_code
        ):
            raise ValueError("country_code must be two upper-case letters.")
        safe_identifier(self.build_id, label="build_id")
        if not isinstance(self.licensed_data, bool):
            raise ValueError("licensed_data must be True or False.")
        if self.release is not None:
            safe_identifier(self.release, label="release")

    def message(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code,
            "build_id": self.build_id,
            "licensed_data": self.licensed_data,
            "release": self.release,
        }

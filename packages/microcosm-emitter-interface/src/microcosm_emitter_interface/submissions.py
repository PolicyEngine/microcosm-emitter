"""What the emitter needs to submit handed-over files to the collector.

The collector's artifact intake (PolicyEngine/microcosm-collector,
``docs/artifact-submissions.md``) records which build produced each
submission. A bundle hand-over says so with ``CollectorProvenance``; without
it, the bundle goes only to Hugging Face. The emitter submits only while its
collector submissions are switched on.

``CollectorSubmissionReceipt`` is what ``EmitterClient.collector_submission_receipt``
answers: the emitter's latest record of what the collector made of a bundle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal, NotRequired, TypedDict

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
            error_message = "country_code must be two upper-case letters."
            raise ValueError(error_message)
        safe_identifier(self.build_id, label="build_id")
        if not isinstance(self.licensed_data, bool):
            error_message = "licensed_data must be True or False."
            # The published constructors raise ValueError for every invalid value.
            raise ValueError(error_message)  # noqa: TRY004
        if self.release is not None:
            safe_identifier(self.release, label="release")

    def message(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code,
            "build_id": self.build_id,
            "licensed_data": self.licensed_data,
            "release": self.release,
        }


class CollectorRejection(TypedDict):
    """The collector's reason for a ``rejected`` or ``failed`` submission.

    The collector's own codes are ``stored_object_mismatch``,
    ``content_mismatch`` and ``object_missing`` (``rejected``), and
    ``worker_error``, ``attempts_exhausted`` and ``worker_unavailable``
    (``failed``); a worker handler may use its own.
    """

    code: str
    message: str


class CollectorSubmission(TypedDict):
    """One submission of a bundle: ``calibration-diagnostics`` or
    ``orrery-inputs``.

    ``status``:

    - ``pending``: not yet accepted; the emitter retries it.
    - ``awaiting_final_status``: completion accepted it; the emitter checks
      its status in the background.
    - ``succeeded``, ``rejected`` or ``failed`` with a ``receipt``: the
      collector's final status, and its receipt. For ``rejected`` and
      ``failed``, ``rejection`` holds the collector's reason and
      ``error_code`` its code.
    - ``failed`` without a ``receipt``: the emitter could not submit it, and
      ``error_code`` says why (for example ``COLLECTOR_REFUSED_422``).
    - ``timed_out``: still not final a day after acceptance; the emitter
      stopped checking. ``collector_status`` is the last status seen, and
      ``error_code`` is ``FINAL_STATUS_TIMED_OUT``, or
      ``COLLECTOR_REFUSED_403`` when the last check was refused because the
      token belongs to another Hugging Face user than the one who submitted
      it.

    ``collector_status`` is the collector's status for the submission
    (``awaiting_upload``, ``queued``, ``processing``, ``succeeded``,
    ``rejected`` or ``failed``) as last reported.
    """

    kind: str
    status: Literal[
        "pending",
        "awaiting_final_status",
        "succeeded",
        "rejected",
        "failed",
        "timed_out",
    ]
    error_code: str | None
    submission_id: str | None
    collector_status: str | None
    rejection: NotRequired[CollectorRejection | None]
    receipt: NotRequired[dict[str, Any] | None]
    status_checks: NotRequired[int]
    #: When completion accepted it, in seconds since the epoch on the
    #: emitter's clock.
    awaiting_final_status_since: NotRequired[float | None]


class CollectorSubmissionReceipt(TypedDict):
    """The emitter's latest record of a bundle hand-over's collector submissions.

    ``status`` sums up its ``submissions``: ``pending`` while one is still
    being sent, then ``awaiting_final_status`` while the emitter checks one's
    status. Once every one is settled it is ``rejected`` if the collector
    rejected one, else ``failed`` if one failed, else ``timed_out`` if one
    timed out, else ``succeeded``; ``error_code`` is then that submission's.
    ``skipped`` means nothing was sent: Hugging Face refused the bundle
    (``NOT_STAGED_ON_HUGGING_FACE``), or its folder is longer than the
    collector accepts (``COLLECTOR_FOLDER_TOO_LONG``).
    """

    handover_id: str
    destination: Literal["collector"]
    status: Literal[
        "pending",
        "awaiting_final_status",
        "succeeded",
        "rejected",
        "failed",
        "timed_out",
        "skipped",
    ]
    error_code: str | None
    attempts: int
    submissions: list[CollectorSubmission]

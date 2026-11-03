"""Best-effort telemetry delivery.

Shared authentication lives outside this component.
"""

from __future__ import annotations

import sys
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from microcosm_emitter.core.auth import (
    NETWORK_FAILURES,
    CollectorSession,
    SessionResult,
    UnreadableLoginError,
    _http_post,
    huggingface_token,
)
from microcosm_emitter.core.collector_origin import resolve_collector_origin
from microcosm_emitter.core.constants import (
    GIVEN_TOKEN_UNUSABLE_MESSAGE,
    INITIAL_RETRY_SECONDS,
    LOCAL_ONLY_MISSING_CREDENTIAL,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_RETRY_SECONDS,
    NO_CREDENTIAL_MESSAGE,
    PENDING_GIVEN_TOKEN_EXPIRED,
    PENDING_GIVEN_TOKEN_REJECTED,
    PENDING_HUGGINGFACE_LOGIN_UNREADABLE,
    REJECTED_CREDENTIAL_MESSAGE,
    RUN_EVENTS_PATH_TEMPLATE,
    RUN_REGISTRATION_PATH,
    UNREADABLE_LOGIN_WAITING_MESSAGE,
    UNSENDABLE_CREDENTIAL_MESSAGE,
)
from microcosm_emitter.telemetry.constants import (
    LOCAL_ONLY_REJECTED_EVENTS,
    REJECTED_EVENTS_MESSAGE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from microcosm_emitter.telemetry.service.spool import EventSpool

# Each print in this module that suppresses Ruff's T201 writes one of its
# messages to standard error on purpose; none is leftover debugging output.

_RETRYABLE_CLIENT_ERRORS = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)
#: Session answers for a given token that can no longer be used, and the
#: reason each pending run records.
_UNUSABLE_GIVEN_TOKEN_REASONS: dict[SessionResult, str] = {
    "token_expired": PENDING_GIVEN_TOKEN_EXPIRED,
    "token_rejected": PENDING_GIVEN_TOKEN_REJECTED,
}


def _permanent_client_error(status: int) -> bool:
    return 400 <= int(status) < 500 and status not in _RETRYABLE_CLIENT_ERRORS


def _registration_key(registration: Mapping[str, Any]) -> str:
    return f"{registration['run_id']}:{registration['producer_id']}"


class CollectorDelivery:
    """Authenticate queued runs and deliver idempotent event batches."""

    def __init__(
        self,
        spool: EventSpool,
        *,
        development_collector_url: str | None = None,
        collector_origin: str | None = None,
        session: CollectorSession | None = None,
    ) -> None:
        """Resolve the collector origin and hold the event queue and the session.

        Without ``session``, a ``CollectorSession`` for that origin is created.
        Raises ``ValueError`` when the origin settings are refused.
        """
        self.collector_url = resolve_collector_origin(
            development_collector_url=development_collector_url,
            collector_origin=collector_origin,
        )
        self.spool = spool
        self.session = session or CollectorSession(
            self.collector_url,
            post=_http_post,
            credential=huggingface_token,
        )
        self._registered: set[str] = set()
        self._warned_no_token = False
        self._warned_unreadable_login = False
        #: Set once the given collector token can no longer be used.
        self._given_token_unusable = False
        #: The runs, as (run ID, producer ID), recorded as waiting for the
        #: saved Hugging Face login to be readable; empty while it can be read.
        self._waiting_for_login: set[tuple[str, str]] = set()
        self._recorded_waiting: set[str] = set()
        self._warned_denied: set[str] = set()
        self._next_attempt_at = 0.0
        self._retry_seconds = INITIAL_RETRY_SECONDS

    @property
    def waiting_for_credential(self) -> bool:
        """Whether this emitter cannot deliver anything, so shutdown stops delivering.

        That is so when the given collector token can no longer be used, or when
        the saved Hugging Face login could not be read at the last attempt.
        """
        return self._given_token_unusable or bool(self._waiting_for_login)

    def flush_once(self) -> bool:
        """Attempt one delivery pass without waiting for retry deadlines."""
        if time.monotonic() < self._next_attempt_at:
            return False
        made_progress = False
        for registration in self.spool.pending_runs():
            run_id = str(registration["run_id"])
            registration_key = _registration_key(registration)
            token = self._collector_token(registration)
            if token is None:
                continue
            if registration_key not in self._registered and not self._register(
                registration, token
            ):
                continue
            events = self.spool.batch(run_id, str(registration["producer_id"]))
            if not events:
                continue
            try:
                status, _ = _http_post(
                    self.collector_url + RUN_EVENTS_PATH_TEMPLATE.format(run_id=run_id),
                    {"events": events},
                    token,
                )
            except NETWORK_FAILURES:
                self._defer_retry()
                continue
            if status == HTTPStatus.ACCEPTED:
                self.spool.acknowledge([event["event_id"] for event in events])
                made_progress = True
                self._retry_seconds = INITIAL_RETRY_SECONDS
            elif status == HTTPStatus.UNAUTHORIZED:
                self.session.invalidate()
            elif status == HTTPStatus.FORBIDDEN:
                self._make_local_only(
                    registration,
                    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
                )
            elif _permanent_client_error(status):
                self._make_local_only(
                    registration,
                    LOCAL_ONLY_REJECTED_EVENTS,
                    message=REJECTED_EVENTS_MESSAGE.format(status=int(status)),
                )
            else:
                self._defer_retry()
        return made_progress

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        try:
            result, token = self.session.credential()
        except UnreadableLoginError as error:
            self._wait_for_readable_login(registration, error.error_type)
            return None
        if self._waiting_for_login:
            # The session raised until it could read the login, so it can now,
            # whatever the exchange answered.
            self._end_wait_for_readable_login()
        if result == "missing":
            if not self._warned_no_token:
                print(NO_CREDENTIAL_MESSAGE, file=sys.stderr, flush=True)  # noqa: T201
                self._warned_no_token = True
            self._make_local_only(registration, LOCAL_ONLY_MISSING_CREDENTIAL)
        elif result == "rejected":
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_CREDENTIAL)
        elif result == "unsendable":
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_CREDENTIAL,
                message=UNSENDABLE_CREDENTIAL_MESSAGE,
            )
        elif result in _UNUSABLE_GIVEN_TOKEN_REASONS:
            self._keep_pending(registration, _UNUSABLE_GIVEN_TOKEN_REASONS[result])
        elif result != "ok":
            self._defer_retry()
        return token

    def _wait_for_readable_login(
        self, registration: Mapping[str, Any], error_type: str
    ) -> None:
        """Keep the run pending while the saved login cannot be read.

        Pending, not local-only, which cannot be undone and would also apply
        to every other pending run in the event queue file: the login exists
        and can be repaired, and then this emitter or a later one delivers
        the run. The login is read again after the backoff, as the exchange
        is after an outage. The warning names only the error's type, once.
        """
        if not self._warned_unreadable_login:
            print(  # noqa: T201
                UNREADABLE_LOGIN_WAITING_MESSAGE.format(error_type=error_type),
                file=sys.stderr,
                flush=True,
            )
            self._warned_unreadable_login = True
        run = (str(registration["run_id"]), str(registration["producer_id"]))
        if run not in self._waiting_for_login:
            self.spool.record_pending_reason(*run, PENDING_HUGGINGFACE_LOGIN_UNREADABLE)
            self._waiting_for_login.add(run)
        self._defer_retry()

    def _end_wait_for_readable_login(self) -> None:
        """Clear the pending reason of every run that waited for the saved login.

        Runs are cleared whether they are registered or not, so that delivery
        during shutdown retries as usual.
        """
        for run_id, producer_id in self._waiting_for_login:
            self.spool.record_pending_reason(run_id, producer_id, None)
        self._waiting_for_login.clear()

    def _keep_pending(self, registration: Mapping[str, Any], reason: str) -> None:
        """Leave the run for an emitter with a credential, saying why once."""
        if not self._given_token_unusable:
            print(GIVEN_TOKEN_UNUSABLE_MESSAGE, file=sys.stderr, flush=True)  # noqa: T201
            self._given_token_unusable = True
        registration_key = _registration_key(registration)
        if registration_key not in self._recorded_waiting:
            self.spool.record_pending_reason(
                str(registration["run_id"]), str(registration["producer_id"]), reason
            )
            self._recorded_waiting.add(registration_key)

    def _register(self, registration: Mapping[str, Any], token: str) -> bool:
        registration_key = _registration_key(registration)
        try:
            status, _ = _http_post(
                self.collector_url + RUN_REGISTRATION_PATH,
                registration,
                token,
            )
        except NETWORK_FAILURES:
            self._defer_retry()
            return False
        if status == HTTPStatus.CREATED:
            self._registered.add(registration_key)
            self._retry_seconds = INITIAL_RETRY_SECONDS
            # A run left by an emitter whose given token became unusable, or
            # that could not read its saved login.
            self.spool.record_pending_reason(
                str(registration["run_id"]), str(registration["producer_id"]), None
            )
            return True
        if status == HTTPStatus.UNAUTHORIZED:
            self.session.invalidate()
        elif status in {
            HTTPStatus.FORBIDDEN,
            HTTPStatus.CONFLICT,
        } or _permanent_client_error(status):
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_REGISTRATION)
        else:
            self._defer_retry()
        return False

    def _make_local_only(
        self,
        registration: Mapping[str, Any],
        reason: str,
        *,
        message: str = REJECTED_CREDENTIAL_MESSAGE,
    ) -> None:
        run_id = str(registration["run_id"])
        producer_id = str(registration["producer_id"])
        registration_key = _registration_key(registration)
        self.spool.make_local_only(run_id, producer_id, reason)
        if reason != LOCAL_ONLY_MISSING_CREDENTIAL:
            self._warn_denied(registration_key, message)

    def _defer_retry(self) -> None:
        self._next_attempt_at = time.monotonic() + self._retry_seconds
        self._retry_seconds = min(MAX_RETRY_SECONDS, self._retry_seconds * 2)

    def _warn_denied(self, registration_key: str, message: str) -> None:
        if registration_key in self._warned_denied:
            return
        print(message, file=sys.stderr, flush=True)  # noqa: T201
        self._warned_denied.add(registration_key)

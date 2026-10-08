"""Best-effort telemetry delivery; shared authentication lives outside this component."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from microcosm_provider_core.auth import (
    CollectorSession,
    _collector_origin,
    _development_collector_url,
    _http_post,
    _huggingface_token,
)
from microcosm_provider_core.constants import (
    INITIAL_RETRY_SECONDS,
    LOCAL_ONLY_MISSING_CREDENTIAL,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_RETRY_SECONDS,
    NO_CREDENTIAL_MESSAGE,
    PRODUCTION_COLLECTOR_URL,
    REJECTED_CREDENTIAL_MESSAGE,
    RUN_EVENTS_PATH_TEMPLATE,
    RUN_REGISTRATION_PATH,
)

from microcosm_provider_telemetry.service.spool import EventSpool


def _registration_key(registration: Mapping[str, Any]) -> str:
    return f"{registration['run_id']}:{registration['producer_id']}"


class CollectorDelivery:
    """Authenticate queued runs and deliver idempotent event batches."""

    def __init__(
        self,
        spool: EventSpool,
        *,
        development_collector_url: str | None = None,
        session: CollectorSession | None = None,
    ) -> None:
        self.collector_url = (
            _development_collector_url(development_collector_url)
            if development_collector_url is not None
            else _collector_origin(PRODUCTION_COLLECTOR_URL)
        )
        self.spool = spool
        self.session = session or CollectorSession(
            self.collector_url,
            post=lambda *args: _http_post(*args),
            credential=lambda: _huggingface_token(),
        )
        self._registered: set[str] = set()
        self._warned_no_token = False
        self._warned_denied: set[str] = set()
        self._next_attempt_at = 0.0
        self._retry_seconds = INITIAL_RETRY_SECONDS

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
            if registration_key not in self._registered:
                if not self._register(registration, token):
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
            except (OSError, TimeoutError):
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
            else:
                self._defer_retry()
        return made_progress

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        result, token = self.session_credential()
        if result == "missing":
            if not self._warned_no_token:
                print(NO_CREDENTIAL_MESSAGE, file=sys.stderr, flush=True)
                self._warned_no_token = True
            self._make_local_only(registration, LOCAL_ONLY_MISSING_CREDENTIAL)
        elif result == "rejected":
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_CREDENTIAL)
        elif result != "ok":
            self._defer_retry()
        return token

    def session_credential(self) -> tuple[str, str | None]:
        """Compatibility accessor; authentication has no telemetry queue policy."""
        return self.session.credential()

    def _register(self, registration: Mapping[str, Any], token: str) -> bool:
        registration_key = _registration_key(registration)
        try:
            status, _ = _http_post(
                self.collector_url + RUN_REGISTRATION_PATH,
                registration,
                token,
            )
        except (OSError, TimeoutError):
            self._defer_retry()
            return False
        if status == HTTPStatus.CREATED:
            self._registered.add(registration_key)
            self._retry_seconds = INITIAL_RETRY_SECONDS
            return True
        if status == HTTPStatus.UNAUTHORIZED:
            self.session.invalidate()
        elif status in {HTTPStatus.FORBIDDEN, HTTPStatus.CONFLICT}:
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_REGISTRATION)
        else:
            self._defer_retry()
        return False

    def _make_local_only(
        self,
        registration: Mapping[str, Any],
        reason: str,
    ) -> None:
        run_id = str(registration["run_id"])
        producer_id = str(registration["producer_id"])
        registration_key = _registration_key(registration)
        self.spool.make_local_only(run_id, producer_id, reason)
        if reason != LOCAL_ONLY_MISSING_CREDENTIAL:
            self._warn_denied(registration_key)

    def _defer_retry(self) -> None:
        self._next_attempt_at = time.monotonic() + self._retry_seconds
        self._retry_seconds = min(MAX_RETRY_SECONDS, self._retry_seconds * 2)

    def _warn_denied(self, registration_key: str) -> None:
        if registration_key in self._warned_denied:
            return
        print(REJECTED_CREDENTIAL_MESSAGE, file=sys.stderr, flush=True)
        self._warned_denied.add(registration_key)

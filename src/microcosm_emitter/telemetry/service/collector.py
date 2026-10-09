"""Collector authentication and best-effort event delivery."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from huggingface_hub import get_token

from microcosm_emitter.telemetry.service.constants import (
    COLLECTOR_RESPONSE_TOO_LARGE_ERROR,
    COLLECTOR_URL_HTTPS_ERROR,
    COLLECTOR_URL_ORIGIN_ERROR,
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR,
    HTTP_TIMEOUT_SECONDS,
    HTTP_USER_AGENT,
    INITIAL_RETRY_SECONDS,
    LOCAL_ONLY_MISSING_CREDENTIAL,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    LOOPBACK_HOSTS,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_RETRY_SECONDS,
    MINIMUM_TOKEN_LIFETIME_SECONDS,
    NO_CREDENTIAL_MESSAGE,
    PRODUCTION_COLLECTOR_URL,
    REJECTED_CREDENTIAL_MESSAGE,
    RUN_EVENTS_PATH_TEMPLATE,
    RUN_REGISTRATION_PATH,
    TOKEN_EXCHANGE_PATH,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from microcosm_emitter.telemetry.service.spool import EventSpool


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the explicitly configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _collector_origin(value: str, *, allow_loopback_http: bool = False) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in LOOPBACK_HOSTS
    valid_scheme = parsed.scheme == "https" or (
        allow_loopback_http and loopback and parsed.scheme == "http"
    )
    if not parsed.hostname or not valid_scheme:
        raise ValueError(COLLECTOR_URL_HTTPS_ERROR)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(COLLECTOR_URL_ORIGIN_ERROR)
    return value.rstrip("/")


def _development_collector_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR)
    return _collector_origin(value, allow_loopback_http=True)


def _decode_response(body: bytes) -> dict[str, Any]:
    try:
        response = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return response if isinstance(response, dict) else {}


def _http_post(
    url: str,
    payload: Mapping[str, Any],
    bearer_token: str,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                raise OSError(COLLECTOR_RESPONSE_TOO_LARGE_ERROR)
            return response.status, _decode_response(body)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            return error.code, {}
        return error.code, _decode_response(body)


def _huggingface_token() -> str | None:
    return (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGINGFACE_TOKEN", "").strip()
        or get_token()
    )


def _registration_key(registration: Mapping[str, Any]) -> str:
    return f"{registration['run_id']}:{registration['producer_id']}"


class CollectorDelivery:
    """Authenticate queued runs and deliver idempotent event batches."""

    def __init__(
        self,
        spool: EventSpool,
        *,
        development_collector_url: str | None = None,
    ) -> None:
        self.collector_url = (
            _development_collector_url(development_collector_url)
            if development_collector_url is not None
            else _collector_origin(PRODUCTION_COLLECTOR_URL)
        )
        self.spool = spool
        self._session_token: tuple[str, float] | None = None
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
                self._session_token = None
            elif status == HTTPStatus.FORBIDDEN:
                self._make_local_only(
                    registration,
                    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
                )
            else:
                self._defer_retry()
        return made_progress

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        if (
            self._session_token is not None
            and self._session_token[1] > time.monotonic() + TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._session_token[0]
        hf_token = _huggingface_token()
        if not hf_token:
            if not self._warned_no_token:
                print(NO_CREDENTIAL_MESSAGE, file=sys.stderr, flush=True)
                self._warned_no_token = True
            self._make_local_only(registration, LOCAL_ONLY_MISSING_CREDENTIAL)
            return None
        try:
            status, response = _http_post(
                self.collector_url + TOKEN_EXCHANGE_PATH,
                {},
                hf_token,
            )
        except (OSError, TimeoutError):
            self._defer_retry()
            return None
        if status == HTTPStatus.OK and isinstance(response.get("access_token"), str):
            expires_in = max(
                MINIMUM_TOKEN_LIFETIME_SECONDS,
                int(response.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)),
            )
            token = response["access_token"]
            self._session_token = (token, time.monotonic() + expires_in)
            return token
        if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_CREDENTIAL)
        else:
            self._defer_retry()
        return None

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
            self._session_token = None
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

"""Collector authentication and best-effort event delivery."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
    LEASE_SWEEP_INTERVAL_SECONDS,
    LOCAL_ONLY_MISSING_CREDENTIAL,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_EVENTS,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    LOOPBACK_HOSTS,
    MAX_EVENTS_REQUEST_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_RETRY_SECONDS,
    MINIMUM_TOKEN_LIFETIME_SECONDS,
    NO_CREDENTIAL_MESSAGE,
    ORPHAN_IDLE_SECONDS,
    OWN_LEASE_TIMEOUT_SECONDS,
    PRODUCTION_COLLECTOR_URL,
    REJECTED_CREDENTIAL_MESSAGE,
    REJECTED_EVENTS_MESSAGE,
    REJECTED_REGISTRATION_MESSAGE,
    REJECTED_RUN_USER_MESSAGE,
    RUN_EVENTS_PATH_TEMPLATE,
    RUN_REGISTRATION_PATH,
    TOKEN_EXCHANGE_PATH,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from microcosm_emitter.telemetry.service.leases import (
    OwnLeaseUnavailableError,
    ProducerLease,
    ProducerLeases,
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


def _request_body(payload: Mapping[str, Any]) -> bytes:
    """Serialize a request body: the bytes that are sent and that are measured."""

    return json.dumps(payload, separators=(",", ":")).encode()


#: An events request holding no events: what every batch is wrapped in.
_EVENTS_ENVELOPE_BYTES = len(_request_body({"events": []}))


def _events_fitting_request(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the longest run of leading events whose request body fits.

    The collector refuses a larger body whole, which would settle the run as
    rejected. The first event is always kept: a request is never empty, and an
    event too large to send alone is the collector's to refuse.
    """

    size = _EVENTS_ENVELOPE_BYTES
    for index, event in enumerate(events):
        # After the first, each event also costs the comma before it.
        size += len(_request_body(event)) + (1 if index else 0)
        if index and size > MAX_EVENTS_REQUEST_BYTES:
            return events[:index]
    return events


def _http_post(
    url: str,
    payload: Mapping[str, Any],
    bearer_token: str,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=_request_body(payload),
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


#: Client errors worth retrying: a timeout and a rate limit pass with time.
#: Every other 4xx is the collector's settled answer to the same request, so
#: retrying it only wedges the queue (a payload shape it does not accept, say).
_RETRYABLE_CLIENT_ERRORS = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)


def _permanent_client_error(status: int) -> bool:
    return 400 <= int(status) < 500 and status not in _RETRYABLE_CLIENT_ERRORS


def _run(registration: Mapping[str, Any]) -> tuple[str, str]:
    """Identify a producer's run. Ids may contain any separator, so the pair
    itself is the key: joined into one string, two runs could share it."""

    return str(registration["run_id"]), str(registration["producer_id"])


def _credential_fingerprint(hf_token: str) -> str:
    """Identify a credential in memory without keeping the token itself."""

    return hashlib.sha256(hf_token.encode()).hexdigest()


class CollectorDelivery:
    """Authenticate queued runs and deliver idempotent event batches.

    A service delivers its own producer's run. The spool is shared by every
    build on the host, so it also holds other producers' runs; the service
    adopts one only once that producer's service has gone (see ``leases``), one
    adopter at a time. The collector gives a run to the Hugging Face user who
    registers it first, and this service's credential is not the other run's,
    so an answer about the credential (none, rejected, or not the run's owner)
    makes only the service's own run local-only. Another producer's run goes
    local-only only for an answer that does not depend on who asks.
    """

    def __init__(
        self,
        spool: EventSpool,
        registration: Mapping[str, Any],
        *,
        development_collector_url: str | None = None,
        leases: ProducerLeases | None = None,
    ) -> None:
        self.collector_url = (
            _development_collector_url(development_collector_url)
            if development_collector_url is not None
            else _collector_origin(PRODUCTION_COLLECTOR_URL)
        )
        self.spool = spool
        self._own_run = _run(registration)
        self._leases = (
            leases if leases is not None else ProducerLeases.beside(spool.path)
        )
        # Taken before the service is ready, so before its run has any event:
        # no other service can find the run pending without a live lease.
        self._own_lease = self._hold_own_lease()
        self._session_token: tuple[str, float] | None = None
        self._registered: set[tuple[str, str]] = set()
        # Runs being registered again because the collector answered that it
        # does not have them; an acknowledged batch clears the run.
        self._registering_again: set[tuple[str, str]] = set()
        self._warned_no_token = False
        self._warned_denied: set[tuple[str, str]] = set()
        self._next_attempt_at = 0.0
        self._retry_seconds = INITIAL_RETRY_SECONDS
        self._next_lease_sweep_at = 0.0
        # Fingerprints of the credential last read and of those the collector
        # refused; the token itself is never kept.
        self._credential: str | None = None
        self._rejected_credentials: set[str] = set()
        # Other producers' runs this credential cannot deliver, by the
        # credential that was refused; tried again once the credential changes.
        self._passed_over: dict[tuple[str, str], str | None] = {}

    def _hold_own_lease(self) -> ProducerLease | None:
        """Take this service's lease, or refuse to deliver without it.

        Other services read a lease file nobody holds as an exited producer's,
        and a failed attempt can leave one behind. A service that served
        without its lease could so have its live run adopted, and registered
        under another login. Only where there are no advisory locks at all does
        a service run without one; other services then judge its run by recent
        activity.
        """

        lease = self._leases.hold(
            *self._own_run,
            timeout_seconds=OWN_LEASE_TIMEOUT_SECONDS,
        )
        if lease is None and self._leases.supported:
            raise OwnLeaseUnavailableError(self._leases.path(*self._own_run))
        return lease

    def close(self) -> None:
        """Release this service's lease; its run, if still pending, is adoptable."""

        if self._own_lease is not None:
            self._own_lease.release()

    def flush_once(self) -> bool:
        """Attempt one delivery pass without waiting for retry deadlines."""

        if time.monotonic() < self._next_attempt_at:
            return False
        made_progress = False
        pending = self.spool.pending_runs()
        pending_runs = {_run(registration) for registration in pending}
        self._passed_over = {
            run: credential
            for run, credential in self._passed_over.items()
            if run in pending_runs
        }
        if self._passed_over:
            self._note_current_credential()
        self._sweep_leases_if_due(pending)
        for registration in pending:
            claim = self._claim(registration)
            if claim is None:
                continue
            with claim:
                # The list was read before this pass's requests, which can
                # take seconds. Meanwhile a run's own service may have made it
                # local-only and exited, freeing its lease. Only now, holding
                # the run, is its state settled.
                if not self.spool.run_has_deliverable(*_run(registration)):
                    continue
                if self._deliver(registration):
                    made_progress = True
        return made_progress

    def _claim(
        self,
        registration: Mapping[str, Any],
    ) -> contextlib.AbstractContextManager[object] | None:
        """Return a hold on a run this service may deliver now, else ``None``.

        The own run is always deliverable. Another producer's run is
        deliverable only while this service holds that producer's lease, which
        it can take only once the producing service has gone. A run with no
        lease file (an older checkout's, or one whose free lease was swept)
        is first left alone until it has been idle for ``ORPHAN_IDLE_SECONDS``.
        """

        run = _run(registration)
        if run == self._own_run:
            return contextlib.nullcontext()
        if run in self._passed_over and self._passed_over[run] == self._credential:
            return None
        if not self._leases.supported:
            return contextlib.nullcontext() if self._idle(run) else None
        try:
            try:
                return self._leases.try_acquire(*run, create=False)
            except FileNotFoundError:
                if not self._idle(run):
                    return None
                return self._leases.try_acquire(*run, create=True)
        except OSError:
            # A lease that cannot be tested may be live: leave the run be.
            return None

    def _note_current_credential(self) -> None:
        """Record the credential this pass's requests would be sent under.

        A run passed over with one credential is offered again once the login
        changes. Delivering a run is what reads the login, and a passed-over run
        is not delivered, so with nothing else pending the change would go
        unseen. A session the collector issued stays in use until it expires,
        as for the own run, so only then does a new login count.
        """

        if self._cached_session_token() is not None:
            return
        hf_token = _huggingface_token()
        self._credential = _credential_fingerprint(hf_token) if hf_token else None

    def _cached_session_token(self) -> str | None:
        if (
            self._session_token is not None
            and self._session_token[1] > time.monotonic() + TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._session_token[0]
        return None

    def _idle(self, run: tuple[str, str]) -> bool:
        updated_at = self.spool.last_updated(*run)
        if updated_at is None:
            return False
        return datetime.now(UTC) - updated_at >= timedelta(seconds=ORPHAN_IDLE_SECONDS)

    def _sweep_leases_if_due(self, pending: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        if now < self._next_lease_sweep_at:
            return
        self._next_lease_sweep_at = now + LEASE_SWEEP_INTERVAL_SECONDS
        keep = {_run(registration) for registration in pending}
        keep.add(self._own_run)
        with contextlib.suppress(OSError):
            self._leases.sweep(keep)

    def _deliver(self, registration: Mapping[str, Any]) -> bool:
        """Send one batch of a run's events; return whether any was delivered."""

        run_id, producer_id = _run(registration)
        token = self._collector_token(registration)
        if token is None:
            return False
        if (run_id, producer_id) not in self._registered:
            if not self._register(registration, token):
                return False
        events = _events_fitting_request(self.spool.batch(run_id, producer_id))
        if not events:
            return False
        try:
            status, _ = _http_post(
                self.collector_url + RUN_EVENTS_PATH_TEMPLATE.format(run_id=run_id),
                {"events": events},
                token,
            )
        except (OSError, TimeoutError):
            self._defer_retry()
            return False
        if status == HTTPStatus.ACCEPTED:
            self.spool.acknowledge([event["event_id"] for event in events])
            self._retry_seconds = INITIAL_RETRY_SECONDS
            self._registering_again.discard(_run(registration))
            return True
        if status == HTTPStatus.UNAUTHORIZED:
            self._session_token = None
        elif status == HTTPStatus.FORBIDDEN:
            # The collector refuses the caller here (another Hugging Face user
            # owns the run, say), so the answer depends on whose credential asked.
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
                caller_dependent=True,
                message=REJECTED_RUN_USER_MESSAGE.format(status=int(status)),
            )
        elif (
            status == HTTPStatus.NOT_FOUND
            and _run(registration) not in self._registering_again
        ):
            # The collector no longer has a run it registered for this service
            # (its database was restored, say). Registering is repeatable for
            # the run's owner, so the next pass registers the run and sends the
            # batch again. Not finding the run once more, with no batch
            # acknowledged in between, is its settled answer.
            self._registered.discard(_run(registration))
            self._registering_again.add(_run(registration))
        elif _permanent_client_error(status):
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_EVENTS,
                caller_dependent=False,
                message=REJECTED_EVENTS_MESSAGE.format(status=int(status)),
            )
        else:
            self._defer_retry()
        return False

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        session_token = self._cached_session_token()
        if session_token is not None:
            return session_token
        hf_token = _huggingface_token()
        if not hf_token:
            self._credential = None
            if _run(registration) == self._own_run and not self._warned_no_token:
                print(NO_CREDENTIAL_MESSAGE, file=sys.stderr, flush=True)
                self._warned_no_token = True
            self._make_local_only(
                registration,
                LOCAL_ONLY_MISSING_CREDENTIAL,
                caller_dependent=True,
                # Said once for the service above, not per run.
                message=None,
            )
            return None
        credential = _credential_fingerprint(hf_token)
        self._credential = credential
        if credential in self._rejected_credentials:
            # The collector has already refused this credential; asking again
            # would get the same answer.
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_CREDENTIAL,
                caller_dependent=True,
                message=REJECTED_CREDENTIAL_MESSAGE,
            )
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
            self._rejected_credentials.add(credential)
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_CREDENTIAL,
                caller_dependent=True,
                message=REJECTED_CREDENTIAL_MESSAGE,
            )
        else:
            self._defer_retry()
        return None

    def _register(self, registration: Mapping[str, Any], token: str) -> bool:
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
            self._registered.add(_run(registration))
            self._retry_seconds = INITIAL_RETRY_SECONDS
            return True
        if status == HTTPStatus.UNAUTHORIZED:
            self._session_token = None
        elif status == HTTPStatus.FORBIDDEN:
            # Another Hugging Face user registered the run first.
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_REGISTRATION,
                caller_dependent=True,
                message=REJECTED_RUN_USER_MESSAGE.format(status=int(status)),
            )
        elif status == HTTPStatus.CONFLICT or _permanent_client_error(status):
            # A conflict reaches only the run's owner, so it is about the
            # registration itself, as is any other settled 4xx.
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_REGISTRATION,
                caller_dependent=False,
                message=REJECTED_REGISTRATION_MESSAGE.format(status=int(status)),
            )
        else:
            self._defer_retry()
        return False

    def _make_local_only(
        self,
        registration: Mapping[str, Any],
        reason: str,
        *,
        caller_dependent: bool,
        message: str | None,
    ) -> None:
        """Stop uploading a run, unless the reason is only this service's.

        ``caller_dependent`` marks an outcome that turns on this service's own
        credential: none, refused, or not the run's owner. That decides the
        service's own run, whose credential it is. Another producer's run stays
        pending for a service whose credential the collector accepts for it,
        and is not offered again with this credential.

        ``message`` is the warning for the service's own run, and has no
        default: each answer says what the collector refused. ``None`` prints
        nothing.
        """

        run = _run(registration)
        own = run == self._own_run
        if caller_dependent and not own:
            if reason != LOCAL_ONLY_MISSING_CREDENTIAL:
                self._passed_over[run] = self._credential
            return
        self.spool.make_local_only(*run, reason)
        # The warnings say "this run": another build's run is not this build's
        # to report, and its reason is kept in the spool.
        if own and message is not None:
            self._warn_denied(run, message)

    def _defer_retry(self) -> None:
        self._next_attempt_at = time.monotonic() + self._retry_seconds
        self._retry_seconds = min(MAX_RETRY_SECONDS, self._retry_seconds * 2)

    def _warn_denied(self, run: tuple[str, str], message: str) -> None:
        if run in self._warned_denied:
            return
        print(message, file=sys.stderr, flush=True)
        self._warned_denied.add(run)

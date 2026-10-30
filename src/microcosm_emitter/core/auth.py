"""HF-to-collector session shared by telemetry and graph publication.

An emitter without a Hugging Face credential, such as one in a Modal
container, can be given a telemetry-only collector token instead, through
the variables named in ``core.constants``; ``CollectorSession`` then uses it,
and sends it only to the collector it was obtained from.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.client import HTTPException
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit

from huggingface_hub import get_token

from microcosm_emitter.core.constants import (
    COLLECTOR_RESPONSE_TOO_LARGE_ERROR,
    COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
    COLLECTOR_TOKEN_ORIGIN_VARIABLE,
    COLLECTOR_TOKEN_VARIABLE,
    COLLECTOR_URL_HTTPS_ERROR,
    COLLECTOR_URL_ORIGIN_ERROR,
    DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR,
    GIVEN_TOKEN_EXPIRY_ERROR,
    GIVEN_TOKEN_ORIGIN_ERROR,
    GIVEN_TOKEN_START_ERROR_MESSAGE,
    GIVEN_TOKEN_VALUE_ERROR,
    GIVEN_TOKEN_VARIABLES_ERROR,
    HTTP_TIMEOUT_SECONDS,
    HTTP_USER_AGENT,
    LOOPBACK_HOSTS,
    MAX_HTTP_RESPONSE_BYTES,
    MINIMUM_TOKEN_LIFETIME_SECONDS,
    TELEMETRY_SCOPE,
    TOKEN_EXCHANGE_PATH,
    TOKEN_REFRESH_MARGIN_SECONDS,
    UNSENDABLE_CREDENTIAL_ERROR,
)

#: Visible ASCII (letters, digits and punctuation), the only characters of a
#: Hugging Face credential or a collector token. Any other value is refused
#: before it is sent: a bearer token (RFC 6750) has no spaces, tabs, line
#: breaks or non-ASCII characters, so the collector would refuse it, and
#: http.client raises for a line break with an error that quotes the whole
#: header value, and for a character outside Latin-1 with one that holds it.
_SENDABLE_CREDENTIAL = re.compile(r"[!-~]+")
#: What ``_http_post`` raises when it gets no usable answer, which every caller
#: handles as a network failure: no answer or a timeout (``OSError``,
#: ``TimeoutError``), or an answer that is not valid HTTP or ends early, such
#: as a truncated chunked answer (http.client's ``HTTPException``, which is not
#: an ``OSError``).
NETWORK_FAILURES = (OSError, TimeoutError, HTTPException)


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
    if not _SENDABLE_CREDENTIAL.fullmatch(bearer_token):
        raise ValueError(UNSENDABLE_CREDENTIAL_ERROR)
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


class UnreadableLoginError(OSError):
    """The saved Hugging Face login could not be read, and no token variable is set.

    The login is the one `hf auth login` saved, which can be a file this user may
    not read or one that is not text; the variables are `HF_TOKEN` and
    `HUGGINGFACE_TOKEN`.

    ``error_type`` is the name of the error ``huggingface_hub`` raised. That
    error is not kept, not even as the context of this one: its text names
    the file, and a ``UnicodeDecodeError`` holds the file's bytes. An
    ``OSError``, as reading a file that fails is, so callers that handled the
    ``PermissionError`` before still handle it.
    """

    def __init__(self, error_type: str) -> None:
        """Keep ``error_type``, and a message that names only that type."""
        super().__init__(
            f"the saved Hugging Face login could not be read ({error_type})"
        )
        self.error_type = error_type


def _saved_login_unreadable(error: OSError | UnicodeDecodeError) -> bool:
    """Return whether ``get_token()`` raised ``error`` reading the saved login files.

    The files are the ones `hf auth login` saved, and such an error is a
    ``PermissionError``, bytes that are not text, or another ``OSError`` that
    names a file, such as ``IsADirectoryError``.

    With ``HF_OIDC_RESOURCE`` set, huggingface_hub exchanges a CI OIDC token
    instead of reading those files, so none of its errors count, such as the
    ``HfHubHTTPError`` (an ``OSError`` that names no file) of a 503 from the
    Hub, or an answer that is not text.
    """
    if os.environ.get("HF_OIDC_RESOURCE"):
        return False
    return isinstance(error, (PermissionError, UnicodeDecodeError)) or (
        isinstance(error, OSError) and error.filename is not None
    )


def huggingface_token() -> str | None:
    """Return the build's Hugging Face token, or `None` without one.

    It is read from `HF_TOKEN`, then `HUGGINGFACE_TOKEN`, skipping blank values,
    then from the login saved by `hf auth login`.

    Raises ``UnreadableLoginError`` when only the saved login is left and it
    cannot be read (``_saved_login_unreadable``); every other error of
    ``get_token()`` propagates unchanged.
    """
    variable_token = (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGINGFACE_TOKEN", "").strip()
    )
    if variable_token:
        return variable_token
    try:
        return get_token()
    except (OSError, UnicodeDecodeError) as error:
        # huggingface_hub catches only FileNotFoundError when it reads the file.
        if not _saved_login_unreadable(error):
            raise
        error_type = type(error).__name__
    # Outside the except block, so the new error has no context to keep.
    raise UnreadableLoginError(error_type)


@dataclass(frozen=True)
class GivenCollectorToken:
    """A telemetry-only collector token obtained on another machine.

    ``expires_at`` is in seconds since the epoch. ``origin`` is the collector
    the token was obtained from; it is only ever compared.
    """

    access_token: str = field(repr=False)
    expires_at: float
    origin: str


class GivenCollectorTokenError(ValueError):
    """The given-token variables are incomplete, unreadable, or name another collector.

    The message names the variables, never their values.
    """


def given_collector_token(
    environment: Mapping[str, str],
) -> GivenCollectorToken | None:
    """Return the collector token in ``environment``, or ``None`` without one."""
    access_token, expires_at, origin = (
        environment.get(name, "").strip()
        for name in (
            COLLECTOR_TOKEN_VARIABLE,
            COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
            COLLECTOR_TOKEN_ORIGIN_VARIABLE,
        )
    )
    if not (access_token or expires_at or origin):
        return None
    if not (access_token and expires_at and origin):
        raise GivenCollectorTokenError(GIVEN_TOKEN_VARIABLES_ERROR)
    if not _SENDABLE_CREDENTIAL.fullmatch(access_token):
        raise GivenCollectorTokenError(GIVEN_TOKEN_VALUE_ERROR)
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        raise GivenCollectorTokenError(GIVEN_TOKEN_EXPIRY_ERROR) from None
    if expiry.tzinfo is None:
        raise GivenCollectorTokenError(GIVEN_TOKEN_EXPIRY_ERROR)
    return GivenCollectorToken(access_token, expiry.timestamp(), origin.rstrip("/"))


#: Why an exchange of a Hugging Face credential gave no token.
ExchangeFailureResult = Literal[
    "missing",
    "unsendable",
    "rejected",
    "unavailable",
    "intake_unavailable",
    "lifetime_unsupported",
]


@dataclass(frozen=True)
class ExchangedToken:
    """A usable answer to an exchange: a 200 with a sendable token and its lifetime.

    ``access_token`` is visible ASCII characters, which can be sent as a bearer
    token, and ``expires_in`` a positive whole number of seconds. Its repr leaves
    the token out.
    """

    access_token: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class ExchangeFailure:
    """An exchange of a Hugging Face credential that gave no token.

    ``result`` is ``missing`` (no Hugging Face credential, so nothing was
    sent); ``unsendable`` (the credential contains characters other than
    visible ASCII, such as a pasted space, which cannot be sent as a bearer
    token, so nothing was sent); ``rejected``
    (401 or 403); ``intake_unavailable`` (the collector has no intake for the
    requested scope: 404, or a 200 for another scope); ``lifetime_unsupported``
    (422 to ``lifetime_seconds``: the collector's strict exchange body
    predates it); or ``unavailable`` (no answer, an answer that is not valid
    HTTP, or one without a usable token). ``status`` is the collector's HTTP
    status, or ``None`` when it did not answer.
    """

    result: ExchangeFailureResult
    status: int | None = None


#: The collector's answer to one exchange of a Hugging Face credential.
TokenExchange = ExchangedToken | ExchangeFailure


def exchange_huggingface_credential(
    origin: str,
    hugging_face_token: str | None,
    *,
    scope: str | None = None,
    lifetime_seconds: int | None = None,
    post: Callable[..., tuple[int, dict[str, Any]]] | None = None,
) -> TokenExchange:
    """Exchange a Hugging Face credential at ``origin`` for a collector token.

    Without ``scope`` the body is ``{}``, which the collector answers with a
    ``telemetry:write`` token. ``lifetime_seconds`` asks for a token that
    lasts that long, for a build on a machine without the credential; the
    collector may grant less. A usable answer is a 200 with an
    ``access_token`` that ``_SENDABLE_CREDENTIAL`` accepts, as the Hugging
    Face credential must be, and a positive integer ``expires_in``. Any other
    token could not be sent as a bearer token, so it counts as no usable
    token: ``unavailable``.
    """
    if not hugging_face_token:
        return ExchangeFailure("missing")
    if not _SENDABLE_CREDENTIAL.fullmatch(hugging_face_token):
        return ExchangeFailure("unsendable")
    body: dict[str, Any] = {} if scope is None else {"scope": scope}
    if lifetime_seconds is not None:
        body["lifetime_seconds"] = lifetime_seconds
    try:
        status, answer = (post or _http_post)(
            origin + TOKEN_EXCHANGE_PATH, body, hugging_face_token
        )
    except NETWORK_FAILURES:
        return ExchangeFailure("unavailable")
    if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        return ExchangeFailure("rejected", status)
    if lifetime_seconds is not None and status == HTTPStatus.UNPROCESSABLE_ENTITY:
        return ExchangeFailure("lifetime_unsupported", status)
    if scope not in (None, TELEMETRY_SCOPE) and (
        status == HTTPStatus.NOT_FOUND
        or (status == HTTPStatus.OK and answer.get("scope") != scope)
    ):
        # A collector released before intake answers 200 with a
        # telemetry:write token, and one with intake switched off 404.
        return ExchangeFailure("intake_unavailable", status)
    access_token = answer.get("access_token")
    expires_in = answer.get("expires_in")
    if (
        status != HTTPStatus.OK
        or not isinstance(access_token, str)
        or not _SENDABLE_CREDENTIAL.fullmatch(access_token)
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        return ExchangeFailure("unavailable", status)
    return ExchangedToken(access_token, expires_in)


#: What ``CollectorSession.credential()`` answers. ``token_expired``,
#: ``token_rejected`` and ``withheld_from_destination`` come only from a given
#: token, which cannot be renewed and is sent only to the collector.
#: ``unsendable`` is a Hugging Face credential that was not sent, because it
#: contains characters that cannot be sent as a bearer token. The telemetry
#: module and collector submissions handle it as ``rejected``, the collector's
#: answer before the emitter checked credentials; the Orrery module's jobs
#: record it as ``credential_unsendable``.
SessionResult = Literal[
    "ok",
    "missing",
    "unsendable",
    "rejected",
    "unavailable",
    "intake_unavailable",
    "token_expired",
    "token_rejected",
    "withheld_from_destination",
]
#: The session's answer to each exchange that gave no token. The session
#: never asks for a lifetime, so ``lifetime_unsupported`` does not occur; it
#: would count as an answer without a usable token.
_FAILED_EXCHANGE_ANSWERS: dict[ExchangeFailureResult, SessionResult] = {
    "missing": "missing",
    "unsendable": "unsendable",
    "rejected": "rejected",
    "unavailable": "unavailable",
    "intake_unavailable": "intake_unavailable",
    "lifetime_unsupported": "unavailable",
}


class SessionCredential(NamedTuple):
    """A session's answer; ``token`` is set only when ``result`` is ``ok``.

    It unpacks as ``result, token``. Its repr leaves the token out.
    """

    result: SessionResult
    token: str | None = None

    def __repr__(self) -> str:
        """Show only ``result``, never the token."""
        return f"SessionCredential(result={self.result!r})"


class CollectorSession:
    """One thread-safe short-lived credential shared by independent publishers.

    ``scope`` asks the exchange for a credential with that scope, such as
    ``artifacts:submit``; ``exchange_huggingface_credential`` says what each
    answer means. The session reuses a token until shortly before it expires.

    With a token given in the process environment (see
    ``given_collector_token``), the session uses it and never
    reads or exchanges a Hugging Face credential. The given token only writes
    telemetry, so a session for any other scope has no credential
    (``missing``). It is sent only to ``origin``, the collector it was
    obtained from: for a caller that sends its credential to another
    ``destination``, such as the Orrery module's publication origin, the
    session withholds it on purpose (``withheld_from_destination``, which
    Orrery's jobs record as ``credential_withheld_from_destination``);
    ``destination`` matters only for a given token. It cannot be renewed: from ``TOKEN_REFRESH_MARGIN_SECONDS``
    before it expires, or after ``invalidate()`` (a 401), the session answers
    ``token_expired`` or ``token_rejected``. A given token obtained from
    another origin than ``origin`` raises ``GivenCollectorTokenError``: its
    origin is only compared, and never chooses where anything is sent.

    Without a given token, ``credential()`` raises the ``UnreadableLoginError``
    of ``huggingface_token`` when the saved login cannot be read; the
    telemetry module keeps its runs pending with the reason
    ``huggingface_login_unreadable`` and reads the login again after a
    backoff.
    """

    def __init__(
        self,
        origin: str,
        *,
        post=None,
        credential=None,
        scope=None,
        destination: str | None = None,
    ):
        """Hold the session's settings and read any given token from ``os.environ``.

        ``post`` defaults to ``_http_post`` and ``credential`` to
        ``huggingface_token``; ``destination`` defaults to ``origin``. Raises
        ``GivenCollectorTokenError`` when the given-token variables cannot be used
        or name another collector than ``origin``.
        """
        import threading

        self.origin = origin
        self.scope = scope
        self._destination = origin if destination is None else destination.rstrip("/")
        self._post = post or _http_post
        self._credential = credential or huggingface_token
        self._session_token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self.given_token = given_collector_token(os.environ)
        if self.given_token is not None and self.given_token.origin != origin:
            raise GivenCollectorTokenError(GIVEN_TOKEN_ORIGIN_ERROR)
        self._given_token_rejected = False

    def invalidate(self) -> None:
        """Drop the exchanged token, so that ``credential()`` exchanges again.

        A given token cannot be renewed, so ``credential()`` answers
        ``token_rejected`` for it from then on.
        """
        with self._lock:
            self._session_token = None
            # A given token is never sent again once the collector refused it.
            self._given_token_rejected = self.given_token is not None

    def credential(self) -> SessionCredential:
        """Return the token to send, or the reason there is none.

        An exchanged token is reused until ``TOKEN_REFRESH_MARGIN_SECONDS`` before
        it expires. Raises ``UnreadableLoginError`` when the saved Hugging Face
        login cannot be read.
        """
        with self._lock:
            if self.given_token is not None:
                return self._given_credential(self.given_token)
            if (
                self._session_token is not None
                and self._session_token[1]
                > time.monotonic() + TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return SessionCredential("ok", self._session_token[0])
            exchange = exchange_huggingface_credential(
                self.origin, self._credential(), scope=self.scope, post=self._post
            )
            if isinstance(exchange, ExchangeFailure):
                return SessionCredential(_FAILED_EXCHANGE_ANSWERS[exchange.result])
            lifetime = max(MINIMUM_TOKEN_LIFETIME_SECONDS, exchange.expires_in)
            self._session_token = (exchange.access_token, time.monotonic() + lifetime)
            return SessionCredential("ok", exchange.access_token)

    def _given_credential(self, given: GivenCollectorToken) -> SessionCredential:
        if self.scope not in (None, TELEMETRY_SCOPE):
            return SessionCredential("missing")
        if self._destination != self.origin:
            return SessionCredential("withheld_from_destination")
        if self._given_token_rejected:
            return SessionCredential("token_rejected")
        # The collector allows no clock leeway, so stop as early as an
        # exchanged token is renewed: a request in the token's last seconds
        # would get a 401 and count as a refusal.
        if time.time() + TOKEN_REFRESH_MARGIN_SECONDS >= given.expires_at:
            return SessionCredential("token_expired")
        return SessionCredential("ok", given.access_token)


def shared_session(
    services: dict, origin: str, *, destination: str | None = None
) -> CollectorSession:
    """One module's collector session, without adding auth to the host.

    Modules share one session, and so one exchanged token and its renewal.
    A given token cannot be renewed, so each module gets a session of its
    own for it: a 401 that one module receives ends the token for that
    module only. ``destination`` is the origin the module sends the
    credential to, when that is not the collector.

    Unusable given-token variables raise ``GivenCollectorTokenError``, which
    stops the whole host at start, so this prints the reason first: the
    host prints only a fixed startup failure.
    """
    try:
        session = CollectorSession(origin, destination=destination)
    except GivenCollectorTokenError as error:
        print(
            GIVEN_TOKEN_START_ERROR_MESSAGE.format(reason=error),
            file=sys.stderr,
            flush=True,
        )
        raise
    if session.given_token is not None:
        return session
    shared = services.setdefault("collector_session", session)
    if not isinstance(shared, CollectorSession) or shared.origin != origin:
        error_message = "Provider modules must use the same collector origin."
        raise ValueError(error_message)
    return shared

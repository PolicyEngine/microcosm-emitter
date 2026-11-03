"""A loopback stand-in for the collector's artifact intake and its bucket.

It follows the submission contract (PolicyEngine/microcosm-collector,
``docs/artifact-submissions.md`` on ``main``, served by collector#12's
``intake_api.py``) closely enough to drive the emitter: the scoped token
exchange, creation with signed upload URLs, single-creation uploads,
completion with problems, the status document and the receipt, and status
codes the tests choose.

``finish`` plays the collector's worker: it moves a submission to a status
the test names (``processing``, or a final ``succeeded``, ``rejected`` or
``failed``), with the handler's ``result`` or a ``rejection``.
``hold_status_checks`` makes status requests wait, as ``hold_uploads`` does
for uploads.

``intake`` chooses the collector the tests talk to: ``"on"``, ``"off"`` (the
exchange and every submission path answer 404 ``intake_not_enabled``) or
``"absent"`` (a collector released before intake: the exchange answers 200
with a ``telemetry:write`` token and no ``scope``, and submission paths 404
without a ``code``).

The exchange takes ``lifetime_seconds`` with ``telemetry:write`` only, as
microcosm-collector's exchange for builds on other machines does, and grants
at most ``MAX_EXCHANGE_LIFETIME_SECONDS``. With ``accepts_lifetime_seconds``
false it answers a body with that field 422, as a collector before that
change does: its exchange body forbids unknown fields.

For slow links: ``receive_buffer_bytes`` gives each connection a small
receive buffer, so the client's sends wait for the fake to read, and
``upload_read_pace`` makes it read an upload's body a few bytes at a time.

``tls_uploads`` adds a second server for uploads that speaks TLS, as Cloud
Storage does, while the collector itself stays plain HTTP. A TLS send writes
its whole buffer before it returns, so a slow reader shows how long each
piece of an upload takes. The server has no certificate: it and
``client_tls_context`` use only anonymous cipher suites, which is enough to
put a TLS socket under the client and needs no key material. It listens on a
Unix socket (``upload_socket_path``), which a test client connects the
``https://127.0.0.1`` upload URLs to: a loopback TCP connection on macOS
grows its receive buffer to hundreds of KiB whatever ``SO_RCVBUF`` says,
which hides the pace of reads, and a Unix socket keeps the buffer it is
given.
"""

from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

#: Anonymous key exchange only: no certificate on either side.
ANONYMOUS_CIPHERS = "aNULL:@SECLEVEL=0"
#: The longest token lifetime the exchange grants on request.
MAX_EXCHANGE_LIFETIME_SECONDS = 86_400


def anonymous_tls_context(purpose: int) -> ssl.SSLContext:
    """Return a TLS 1.2 context that negotiates only anonymous cipher suites."""
    context = ssl.SSLContext(purpose)
    if purpose == ssl.PROTOCOL_TLS_CLIENT:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(ANONYMOUS_CIPHERS)
    return context


class _CollectorRequestHandler(BaseHTTPRequestHandler):
    """Answer intake and upload requests from one ``FakeCollector``'s state."""

    #: The fake whose state the requests read and change; each fake sets it on
    #: a subclass of its own.
    collector: ClassVar[FakeCollector]

    def log_message(self, *arguments: Any) -> None:
        pass

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _paced_body(self) -> bytes:
        remaining = int(self.headers.get("Content-Length", "0"))
        pace = self.collector.upload_read_pace
        if pace is None:
            return self.rfile.read(remaining)
        bytes_per_read, pause_seconds = pace
        pieces = []
        while remaining > 0:
            piece = self.rfile.read(min(bytes_per_read, remaining))
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
            time.sleep(pause_seconds)
        return b"".join(pieces)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _token_scope(self) -> str | None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.collector._token_expiry.get(token, float("inf")) <= time.monotonic():
            return None
        return self.collector._tokens.get(token)

    def _refuse_unless_allowed(self) -> bool:
        """Answer 404, 401 or 403 unless intake and the token allow the request.

        Returns whether it answered.
        """
        if self.collector.intake != "on":
            self.collector._intake_not_available(self)
            return True
        scope = self._token_scope()
        if scope is None:
            self._json(401, {"detail": "invalid"})
            return True
        if scope != "artifacts:submit":
            # The token's scope does not allow the request.
            self._json(403, {"detail": "scope"})
            return True
        return False

    def do_POST(self) -> None:
        body = self._body()
        with self.collector._lock:
            if self.path == "/v1/auth/huggingface/exchange":
                return self.collector._exchange(self, body)
            if self._refuse_unless_allowed():
                return None
            if self.path == "/v1/submissions":
                return self.collector._create(self, json.loads(body))
            if self.path.endswith("/complete"):
                return self.collector._complete(self, self.path.split("/")[3])
        self._json(404, {"detail": "Not Found"})
        return None

    def do_GET(self) -> None:
        if self.path.startswith("/v1/submissions/"):
            self.collector.status_check_started.set()
            self.collector.status_checks_released.wait(30)
        with self.collector._lock:
            if self._refuse_unless_allowed():
                return None
            parts = self.path.split("/")
            if len(parts) == 4 and parts[1:3] == ["v1", "submissions"]:
                return self.collector._status(self, parts[3])
            if len(parts) == 5 and parts[4] == "receipt":
                return self.collector._receipt(self, parts[3])
        self._json(404, {"detail": "Not Found"})
        return None

    def do_PUT(self) -> None:
        self.collector.upload_started.set()
        self.collector.uploads_released.wait(30)
        body = self._paced_body()
        with self.collector._lock:
            self.collector._store(self, self.path.removeprefix("/storage/"), body)


def _loopback_server(
    handler_class: type[BaseHTTPRequestHandler], receive_buffer_bytes: int | None
) -> ThreadingHTTPServer:
    """Return an HTTP server bound to a free loopback port, not yet serving."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), handler_class, bind_and_activate=False
    )
    if receive_buffer_bytes is not None:
        # Accepted connections inherit the listening socket's buffer.
        server.socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer_bytes
        )
    server.server_bind()
    server.server_activate()
    return server


class _UnixTlsServer(socketserver.ThreadingUnixStreamServer):
    """Serve TLS on a Unix socket, each connection with the receive buffer given."""

    daemon_threads = True

    def __init__(
        self,
        socket_path: str,
        handler_class: type[BaseHTTPRequestHandler],
        *,
        receive_buffer_bytes: int | None,
        tls_context: ssl.SSLContext,
    ) -> None:
        self._receive_buffer_bytes = receive_buffer_bytes
        self._tls_context = tls_context
        super().__init__(socket_path, handler_class)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        connection, _ = self.socket.accept()
        if self._receive_buffer_bytes is not None:
            connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, self._receive_buffer_bytes
            )
        # The handler expects a (host, port) client address.
        return (
            self._tls_context.wrap_socket(connection, server_side=True),
            ("127.0.0.1", 0),
        )


class FakeCollector:
    def __init__(
        self, *, receive_buffer_bytes: int | None = None, tls_uploads: bool = False
    ) -> None:
        self.exchange_requests: list[dict[str, Any]] = []
        #: The bearer credential of each exchange request.
        self.exchange_credentials: list[str] = []
        self.accepts_lifetime_seconds = True
        self.created: list[dict[str, Any]] = []
        self.completed: list[str] = []
        self.uploads: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.submissions: dict[str, dict[str, Any]] = {}
        self.exchange_status = 200
        #: A whole 200 answer the tests choose for every exchange.
        self.exchange_answer: dict[str, Any] | None = None
        #: Answer every exchange with bytes that are not an HTTP answer.
        self.malformed_exchange_answer = False
        self.intake = "on"
        self.create_statuses: list[int] = []
        self.complete_statuses: list[int] = []
        #: Whole completion answers the tests choose: (status, document).
        self.complete_answers: list[tuple[int, dict[str, Any]]] = []
        #: The submission ID of each status and receipt request, in order.
        self.status_requests: list[str] = []
        self.receipt_requests: list[str] = []
        #: Statuses, optionally with a ``code``, or whole answers
        #: (status, document), the tests choose for status and receipt requests.
        self.status_answers: list[int | tuple[int, Any]] = []
        self.receipt_answers: list[int | tuple[int, Any]] = []
        #: Answer receipts with a ``receipt_sha256`` that does not match them.
        self.corrupt_receipts = False
        #: Set when a status request arrives; status requests wait for
        #: ``status_checks_released`` while ``hold_status_checks`` is on.
        self.status_check_started = threading.Event()
        self.status_checks_released = threading.Event()
        self.status_checks_released.set()
        #: Store each upload with its bytes reversed, so its SHA-256 differs.
        self.corrupt_uploads = False
        self.sign_stored_files = False
        self.upload_origin: str | None = None
        #: A ``Host`` header value for upload instructions other than the URL's.
        self.upload_host: str | None = None
        #: The ``Host`` header each upload arrived with.
        self.upload_hosts: list[str] = []
        #: Each issued session token and its scope.
        self._tokens: dict[str, str] = {}
        #: How long an issued token lasts; an expired one is answered 401.
        self.token_lifetime_seconds = 900
        self._token_expiry: dict[str, float] = {}
        #: Set when an upload arrives; uploads wait for ``uploads_released``
        #: while ``hold_uploads`` is on.
        self.upload_started = threading.Event()
        self.uploads_released = threading.Event()
        self.uploads_released.set()
        #: ``(bytes per read, pause in seconds)``: read upload bodies slowly.
        self.upload_read_pace: tuple[int, float] | None = None
        self._lock = threading.Lock()

        class Handler(_CollectorRequestHandler):
            collector = self

        server_tls_context = anonymous_tls_context(ssl.PROTOCOL_TLS_SERVER)
        self._server = _loopback_server(Handler, receive_buffer_bytes)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._servers: list[socketserver.BaseServer] = [self._server]
        #: The client side of ``tls_uploads``; ``None`` without it.
        self.client_tls_context: ssl.SSLContext | None = None
        #: The Unix socket the TLS upload server listens on.
        self.upload_socket_path: str | None = None
        self._socket_directory: str | None = None
        if tls_uploads:
            self._add_tls_upload_server(
                Handler, server_tls_context, receive_buffer_bytes
            )
        self._threads = [
            threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            for server in self._servers
        ]

    def _add_tls_upload_server(
        self,
        handler_class: type[BaseHTTPRequestHandler],
        server_tls_context: ssl.SSLContext,
        receive_buffer_bytes: int | None,
    ) -> None:
        """Add the TLS upload server on a new Unix socket, and its client context."""
        # A short path: Unix socket paths are limited to about 100 bytes.
        self._socket_directory = tempfile.mkdtemp(prefix="fake-storage-", dir="/tmp")
        # A str: httpx's uds option takes the socket path as a str.
        self.upload_socket_path = str(Path(self._socket_directory) / "tls.sock")
        self._servers.append(
            _UnixTlsServer(
                self.upload_socket_path,
                handler_class,
                receive_buffer_bytes=receive_buffer_bytes,
                tls_context=server_tls_context,
            )
        )
        self.upload_origin = "https://127.0.0.1"
        self.client_tls_context = anonymous_tls_context(ssl.PROTOCOL_TLS_CLIENT)

    def hold_uploads(self) -> None:
        """Make every upload wait until ``release_uploads``."""
        self.uploads_released.clear()

    def release_uploads(self) -> None:
        self.uploads_released.set()

    def hold_status_checks(self) -> None:
        """Make every status and receipt request wait until released."""
        self.status_checks_released.clear()

    def release_status_checks(self) -> None:
        self.status_checks_released.set()

    def finish(
        self,
        submission_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        rejection: dict[str, str] | None = None,
    ) -> None:
        """Move a submission to ``status``, as the collector's worker would."""
        with self._lock:
            submission = self.submissions[submission_id]
            submission["status"] = status
            if submission["job"] is not None:
                submission["job"] = {**submission["job"], "attempts": 1}
            final = status in {"succeeded", "rejected", "failed"}
            submission["finished_at"] = (
                "2026-10-20T09:05:00.000000+00:00" if final else None
            )
            submission["result"] = result if status == "succeeded" else None
            submission["rejection"] = rejection if status != "succeeded" else None

    def start(self) -> FakeCollector:
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        if self.upload_socket_path is not None and self._socket_directory:
            Path(self.upload_socket_path).unlink()
            Path(self._socket_directory).rmdir()

    def _intake_not_available(self, handler: Any) -> None:
        if self.intake == "off":
            return handler._json(
                404, {"detail": "Intake is off.", "code": "intake_not_enabled"}
            )
        handler._json(404, {"detail": "Not Found"})
        return None

    def _chosen_exchange_answer(self, handler: Any) -> bool:
        """Send the exchange answer a test chose, if any; return whether it did."""
        if self.malformed_exchange_answer:
            handler.wfile.write(b"not an HTTP answer\r\n")
            return True
        if self.exchange_status != 200:
            handler._json(self.exchange_status, {"detail": "refused"})
            return True
        if self.exchange_answer is not None:
            handler._json(200, self.exchange_answer)
            return True
        return False

    def _exchange(self, handler: Any, body: bytes) -> None:
        request = json.loads(body) if body else {}
        self.exchange_requests.append(request)
        self.exchange_credentials.append(
            handler.headers.get("Authorization", "").removeprefix("Bearer ")
        )
        if self._chosen_exchange_answer(handler):
            return None
        scope = request.get("scope", "telemetry:write")
        lifetime_seconds = self.token_lifetime_seconds
        if "lifetime_seconds" in request:
            if not self.accepts_lifetime_seconds or scope != "telemetry:write":
                return handler._json(422, {"detail": "invalid exchange body"})
            lifetime_seconds = min(
                request["lifetime_seconds"], MAX_EXCHANGE_LIFETIME_SECONDS
            )
        if scope == "artifacts:submit" and self.intake == "off":
            return self._intake_not_available(handler)
        if self.intake == "absent":
            # Released before intake: every exchange gives a telemetry token.
            token = f"telemetry-token-{len(self._tokens) + 1}"
            self._tokens[token] = "telemetry:write"
            return handler._json(200, {"access_token": token, "expires_in": 900})
        token = f"session-token-{len(self._tokens) + 1}"
        self._tokens[token] = scope
        self._token_expiry[token] = time.monotonic() + lifetime_seconds
        handler._json(
            200,
            {
                "access_token": token,
                "token_type": "bearer",
                "expires_in": lifetime_seconds,
                "scope": scope,
            },
        )
        return None

    @staticmethod
    def _chosen(handler: Any, answer: int | tuple[int, str]) -> None:
        """Answer with a status the test chose, optionally with an error ``code``."""
        status, code = answer if isinstance(answer, tuple) else (answer, None)
        handler._json(status, {"detail": "chosen", **({"code": code} if code else {})})

    def _create(self, handler: Any, body: dict[str, Any]) -> None:
        self.created.append(body)
        if self.create_statuses:
            return self._chosen(handler, self.create_statuses.pop(0))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        submission_id = "sub-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        created = submission_id not in self.submissions
        self.submissions.setdefault(
            submission_id,
            {
                "body": body,
                "status": "awaiting_upload",
                "job": None,
                "rejection": None,
                "result": None,
                "accepted_at": None,
                "finished_at": None,
            },
        )
        submission = self.submissions[submission_id]
        uploads = []
        if submission["status"] == "awaiting_upload":
            origin = self.upload_origin or self.origin
            host = self.upload_host or origin.split("://", 1)[1]
            for file in body["files"]:
                object_name = f"submissions/{submission_id}/{file['name']}"
                if object_name in self.objects and not self.sign_stored_files:
                    continue
                uploads.append(
                    {
                        "name": file["name"],
                        "method": "PUT",
                        "url": f"{origin}/storage/{object_name}",
                        "headers": {
                            "Content-Type": "application/octet-stream",
                            "Host": host,
                            "x-goog-meta-sha256": file["sha256"],
                            "x-goog-content-length-range": (
                                f"{file['size']},{file['size']}"
                            ),
                            "x-goog-if-generation-match": "0",
                        },
                        "expires_at": "2026-10-20T10:00:00+00:00",
                    }
                )
        handler._json(
            201 if created else 200,
            {**self._status_document(submission_id), "uploads": uploads},
        )
        return None

    @staticmethod
    def _files(submission: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(
            (
                {key: file[key] for key in ("name", "role", "size", "sha256")}
                for file in submission["body"]["files"]
            ),
            key=lambda file: file["name"],
        )

    @staticmethod
    def _canonical_sha256(value: Any) -> str:
        """Return the SHA-256 digest of ``value`` in the contract's canonical JSON."""
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _status_document(self, submission_id: str) -> dict[str, Any]:
        """Return the contract's status document for one submission."""
        submission = self.submissions[submission_id]
        return {
            "contract_version": 1,
            "submission_id": submission_id,
            "kind": submission["body"]["kind"],
            "status": submission["status"],
            "files_sha256": self._canonical_sha256(self._files(submission)),
            "created_at": "2026-10-20T09:00:00.000000+00:00",
            "updated_at": "2026-10-20T09:00:00.000000+00:00",
            "accepted_at": submission["accepted_at"],
            "finished_at": submission["finished_at"],
            "job": submission["job"],
            "rejection": submission["rejection"],
        }

    def receipt_document(self, submission_id: str) -> dict[str, Any]:
        """Return the contract's receipt, with its ``receipt_sha256``.

        It is built as collector#12's ``receipt_document`` builds it.
        """
        submission = self.submissions[submission_id]
        body = submission["body"]
        files = self._files(submission)
        if submission["job"] is not None:
            # Completion recorded each object's generation.
            files = [{**file, "generation": "1760866403123456"} for file in files]
        receipt: dict[str, Any] = {
            "contract_version": 1,
            "submission_id": submission_id,
            "kind": body["kind"],
            "status": submission["status"],
            "licensed_data": body["licensed_data"],
            "provenance": body["provenance"],
            "files_sha256": self._canonical_sha256(self._files(submission)),
            "files": files,
            "submitted_by": "fixture-builder",
            "created_at": "2026-10-20T09:00:00.000000+00:00",
            "accepted_at": submission["accepted_at"],
            "finished_at": submission["finished_at"],
            "job": submission["job"],
            "result": submission["result"],
            "rejection": submission["rejection"],
        }
        receipt["receipt_sha256"] = self._canonical_sha256(receipt)
        return receipt

    def _chosen_answer(
        self, handler: Any, answers: list[int | tuple[int, Any]]
    ) -> bool:
        """Send the next answer a test chose, if any."""
        if not answers:
            return False
        answer = answers.pop(0)
        if isinstance(answer, tuple) and isinstance(answer[1], dict):
            handler._json(*answer)
        else:
            self._chosen(handler, answer)
        return True

    def _unknown_submission(self, handler: Any) -> None:
        handler._json(
            404,
            {"detail": "Submission was not found.", "code": "submission_not_found"},
        )

    def _status(self, handler: Any, submission_id: str) -> None:
        self.status_requests.append(submission_id)
        if self._chosen_answer(handler, self.status_answers):
            return None
        if submission_id not in self.submissions:
            return self._unknown_submission(handler)
        handler._json(200, self._status_document(submission_id))
        return None

    def _receipt(self, handler: Any, submission_id: str) -> None:
        self.receipt_requests.append(submission_id)
        if self._chosen_answer(handler, self.receipt_answers):
            return None
        submission = self.submissions.get(submission_id)
        if submission is None:
            return self._unknown_submission(handler)
        if submission["status"] not in {"succeeded", "rejected", "failed"}:
            return handler._json(
                409,
                {
                    "detail": "Submission is not final yet.",
                    "code": "submission_not_final",
                    "status": submission["status"],
                },
            )
        receipt = self.receipt_document(submission_id)
        if self.corrupt_receipts:
            receipt["receipt_sha256"] = "0" * 64
        handler._json(200, receipt)
        return None

    def _store(self, handler: Any, object_name: str, body: bytes) -> None:
        self.uploads.append(object_name)
        self.upload_hosts.append(handler.headers.get("Host", ""))
        expected = handler.headers.get("x-goog-content-length-range", "")
        if expected != f"{len(body)},{len(body)}":
            return handler._json(400, {"detail": "size"})
        if object_name in self.objects:
            return handler._json(412, {"detail": "exists"})
        self.objects[object_name] = body[::-1] if self.corrupt_uploads else body
        handler._json(200, {})
        return None

    def _chosen_completion_answer(self, handler: Any) -> bool:
        """Send the next completion answer or status a test chose, if any.

        Returns whether it sent one.
        """
        if self.complete_answers:
            handler._json(*self.complete_answers.pop(0))
            return True
        if self.complete_statuses:
            self._chosen(handler, self.complete_statuses.pop(0))
            return True
        return False

    def _complete(self, handler: Any, submission_id: str) -> None:
        self.completed.append(submission_id)
        if self._chosen_completion_answer(handler):
            return None
        submission = self.submissions.get(submission_id)
        if submission is None:
            return handler._json(
                404,
                {"detail": "Submission was not found.", "code": "submission_not_found"},
            )
        if submission["status"] in {"rejected", "failed", "succeeded"}:
            return handler._json(200, self._status_document(submission_id))
        problems = []
        mismatched = []
        for file in submission["body"]["files"]:
            stored = self.objects.get(f"submissions/{submission_id}/{file['name']}")
            if stored is None:
                problems.append({"name": file["name"], "problem": "missing"})
            elif hashlib.sha256(stored).hexdigest() != file["sha256"]:
                mismatched.append(file["name"])
        if problems:
            return handler._json(
                409,
                {
                    "detail": "Some files are not stored.",
                    "code": "files_missing",
                    "problems": problems,
                },
            )
        if mismatched:
            # Stored objects are never replaced: the submission is rejected.
            submission["status"] = "rejected"
            submission["rejection"] = {
                "code": "stored_object_mismatch",
                "message": f"{mismatched[0]} has another recorded SHA-256.",
            }
            return handler._json(200, self._status_document(submission_id))
        submission["status"] = "queued"
        submission["accepted_at"] = "2026-10-20T09:01:00.000000+00:00"
        submission["job"] = {
            "job_id": "job-" + submission_id.removeprefix("sub-"),
            "processing_round": 1,
            "attempts": 0,
        }
        handler._json(202, self._status_document(submission_id))
        return None

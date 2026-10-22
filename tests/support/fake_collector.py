"""A loopback stand-in for the collector's artifact intake and its bucket.

It follows the submission contract (PolicyEngine/microcosm-collector,
``docs/artifact-submissions.md`` at ``673df3d``) closely enough to drive the
emitter: the scoped token exchange, creation with signed upload URLs,
single-creation uploads, completion with problems, and status codes the tests
choose.

``intake`` chooses the collector the tests talk to: ``"on"``, ``"off"`` (the
exchange and every submission path answer 404 ``intake_not_enabled``) or
``"absent"`` (a collector released before intake: the exchange answers 200
with a ``telemetry:write`` token and no ``scope``, and submission paths 404
without a ``code``).

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
import os
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Anonymous key exchange only: no certificate on either side.
ANONYMOUS_CIPHERS = "aNULL:@SECLEVEL=0"


def anonymous_tls_context(purpose: int) -> ssl.SSLContext:
    """A TLS 1.2 context that negotiates only anonymous cipher suites."""
    context = ssl.SSLContext(purpose)
    if purpose == ssl.PROTOCOL_TLS_CLIENT:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(ANONYMOUS_CIPHERS)
    return context


class FakeCollector:
    def __init__(
        self, *, receive_buffer_bytes: int | None = None, tls_uploads: bool = False
    ) -> None:
        self.exchange_requests: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.completed: list[str] = []
        self.uploads: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.submissions: dict[str, dict[str, Any]] = {}
        self.exchange_status = 200
        self.intake = "on"
        self.create_statuses: list[int] = []
        self.complete_statuses: list[int] = []
        #: Whole completion answers the tests choose: (status, document).
        self.complete_answers: list[tuple[int, dict[str, Any]]] = []
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
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments: Any) -> None:
                pass

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length", "0")))

            def _paced_body(self) -> bytes:
                remaining = int(self.headers.get("Content-Length", "0"))
                pace = collector.upload_read_pace
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
                if collector._token_expiry.get(token, float("inf")) <= time.monotonic():
                    return None
                return collector._tokens.get(token)

            def do_POST(self) -> None:
                body = self._body()
                with collector._lock:
                    if self.path == "/v1/auth/huggingface/exchange":
                        return collector._exchange(self, body)
                    if collector.intake != "on":
                        return collector._intake_not_available(self)
                    scope = self._token_scope()
                    if scope is None:
                        return self._json(401, {"detail": "invalid"})
                    if scope != "artifacts:submit":
                        # The token's scope does not allow the request.
                        return self._json(403, {"detail": "scope"})
                    if self.path == "/v1/submissions":
                        return collector._create(self, json.loads(body))
                    if self.path.endswith("/complete"):
                        return collector._complete(self, self.path.split("/")[3])
                self._json(404, {"detail": "Not Found"})

            def do_PUT(self) -> None:
                collector.upload_started.set()
                collector.uploads_released.wait(30)
                body = self._paced_body()
                with collector._lock:
                    collector._store(self, self.path.removeprefix("/storage/"), body)

        def loopback_server() -> ThreadingHTTPServer:
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), Handler, bind_and_activate=False
            )
            if receive_buffer_bytes is not None:
                # Accepted connections inherit the listening socket's buffer.
                server.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer_bytes
                )
            server.server_bind()
            server.server_activate()
            return server

        server_tls_context = anonymous_tls_context(ssl.PROTOCOL_TLS_SERVER)

        class UnixTlsServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

            def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
                connection, _ = self.socket.accept()
                if receive_buffer_bytes is not None:
                    connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer_bytes
                    )
                # The handler expects a (host, port) client address.
                return (
                    server_tls_context.wrap_socket(connection, server_side=True),
                    ("127.0.0.1", 0),
                )

        self._server = loopback_server()
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._servers: list[socketserver.BaseServer] = [self._server]
        #: The client side of ``tls_uploads``; ``None`` without it.
        self.client_tls_context: ssl.SSLContext | None = None
        #: The Unix socket the TLS upload server listens on.
        self.upload_socket_path: str | None = None
        self._socket_directory: str | None = None
        if tls_uploads:
            # A short path: Unix socket paths are limited to about 100 bytes.
            self._socket_directory = tempfile.mkdtemp(
                prefix="fake-storage-", dir="/tmp"
            )
            self.upload_socket_path = os.path.join(self._socket_directory, "tls.sock")
            self._servers.append(UnixTlsServer(self.upload_socket_path, Handler))
            self.upload_origin = "https://127.0.0.1"
            self.client_tls_context = anonymous_tls_context(ssl.PROTOCOL_TLS_CLIENT)
        self._threads = [
            threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02},
                daemon=True,
            )
            for server in self._servers
        ]

    def hold_uploads(self) -> None:
        """Make every upload wait until ``release_uploads``."""
        self.uploads_released.clear()

    def release_uploads(self) -> None:
        self.uploads_released.set()

    def start(self) -> FakeCollector:
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        if self.upload_socket_path is not None and self._socket_directory:
            os.unlink(self.upload_socket_path)
            os.rmdir(self._socket_directory)

    def _intake_not_available(self, handler: Any) -> None:
        if self.intake == "off":
            return handler._json(
                404, {"detail": "Intake is off.", "code": "intake_not_enabled"}
            )
        handler._json(404, {"detail": "Not Found"})

    def _exchange(self, handler: Any, body: bytes) -> None:
        request = json.loads(body) if body else {}
        self.exchange_requests.append(request)
        if self.exchange_status != 200:
            return handler._json(self.exchange_status, {"detail": "refused"})
        scope = request.get("scope", "telemetry:write")
        if scope == "artifacts:submit" and self.intake == "off":
            return self._intake_not_available(handler)
        if self.intake == "absent":
            # Released before intake: every exchange gives a telemetry token.
            token = f"telemetry-token-{len(self._tokens) + 1}"
            self._tokens[token] = "telemetry:write"
            return handler._json(200, {"access_token": token, "expires_in": 900})
        token = f"session-token-{len(self._tokens) + 1}"
        self._tokens[token] = scope
        self._token_expiry[token] = time.monotonic() + self.token_lifetime_seconds
        handler._json(
            200,
            {
                "access_token": token,
                "token_type": "bearer",
                "expires_in": self.token_lifetime_seconds,
                "scope": scope,
            },
        )

    @staticmethod
    def _chosen(handler: Any, answer: int | tuple[int, str]) -> None:
        """A status the test chose, optionally with an error ``code``."""
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
            {"body": body, "status": "awaiting_upload", "job": None, "rejection": None},
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

    def _status_document(self, submission_id: str) -> dict[str, Any]:
        """The contract's status document for one submission."""
        submission = self.submissions[submission_id]
        files = sorted(
            (
                {key: file[key] for key in ("name", "role", "size", "sha256")}
                for file in submission["body"]["files"]
            ),
            key=lambda file: file["name"],
        )
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
        return {
            "contract_version": 1,
            "submission_id": submission_id,
            "kind": submission["body"]["kind"],
            "status": submission["status"],
            "files_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "created_at": "2026-10-20T09:00:00.000000+00:00",
            "updated_at": "2026-10-20T09:00:00.000000+00:00",
            "accepted_at": None,
            "finished_at": None,
            "job": submission["job"],
            "rejection": submission["rejection"],
        }

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

    def _complete(self, handler: Any, submission_id: str) -> None:
        self.completed.append(submission_id)
        if self.complete_answers:
            return handler._json(*self.complete_answers.pop(0))
        if self.complete_statuses:
            return self._chosen(handler, self.complete_statuses.pop(0))
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
        submission["job"] = {
            "job_id": "job-" + submission_id.removeprefix("sub-"),
            "processing_round": 1,
            "attempts": 0,
        }
        handler._json(202, self._status_document(submission_id))

"""Configuration and protocol values for the telemetry emitter service."""

from __future__ import annotations

from typing import Final

RETENTION_DAYS: Final = 7
MAX_QUEUED_BYTES: Final = 100 * 1024 * 1024
BATCH_SIZE: Final = 100
PRODUCTION_COLLECTOR_URL: Final = (
    "https://microcosm-telemetry-389282473430.us-central1.run.app"
)

LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
TOKEN_EXCHANGE_PATH: Final = "/v1/auth/huggingface/exchange"
RUN_REGISTRATION_PATH: Final = "/v1/runs"
RUN_EVENTS_PATH_TEMPLATE: Final = "/v1/runs/{run_id}/events"
HTTP_USER_AGENT: Final = "microcosm-telemetry-emitter/1"
MAX_HTTP_RESPONSE_BYTES: Final = 1_048_576
HTTP_TIMEOUT_SECONDS: Final = 5.0

UPLOAD_STATE_PENDING: Final = "pending"
UPLOAD_STATE_LOCAL_ONLY: Final = "local_only"
LOCAL_ONLY_MISSING_CREDENTIAL: Final = "missing_huggingface_credential"
LOCAL_ONLY_REJECTED_CREDENTIAL: Final = "huggingface_credential_rejected"
LOCAL_ONLY_REJECTED_REGISTRATION: Final = "run_registration_rejected"
LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION: Final = "collector_authorization_rejected"
LOCAL_ONLY_PRE_ELIGIBILITY: Final = "created_before_upload_eligibility"

NO_CREDENTIAL_MESSAGE: Final = (
    "Microcosm telemetry is local-only: no ambient Hugging Face credential was "
    "found. The dataset build will continue."
)
REJECTED_CREDENTIAL_MESSAGE: Final = (
    "Microcosm telemetry is local-only for this run: the ambient Hugging Face "
    "credential was not accepted as a PolicyEngine organization member. The "
    "dataset build will continue."
)
COLLECTOR_URL_HTTPS_ERROR: Final = "collector URL must be an HTTPS origin"
COLLECTOR_URL_ORIGIN_ERROR: Final = (
    "collector URL must be an origin without credentials or path data"
)
DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR: Final = (
    "development collector URL must use a loopback address"
)
COLLECTOR_RESPONSE_TOO_LARGE_ERROR: Final = "collector response exceeds 1 MiB"

DEFAULT_TOKEN_LIFETIME_SECONDS: Final = 3_600
MINIMUM_TOKEN_LIFETIME_SECONDS: Final = 60
TOKEN_REFRESH_MARGIN_SECONDS: Final = 30
INITIAL_RETRY_SECONDS: Final = 1.0
MAX_RETRY_SECONDS: Final = 60.0

DATABASE_TIMEOUT_SECONDS: Final = 5
PRUNE_INTERVAL_SECONDS: Final = 60.0

#: Lease files live in ``<spool>.leases/``, one per producer, named by the
#: SHA-256 of ``run_id NUL producer_id``. Microcosm's own telemetry service uses
#: the same layout, so services from both packages sharing one spool recognise
#: each other's leases.
LEASE_DIRECTORY_SUFFIX: Final = ".leases"
LEASE_FILE_SUFFIX: Final = ".lock"
LEASE_RETRY_SECONDS: Final = 0.01
MAX_LEASE_OPEN_ATTEMPTS: Final = 8
OWN_LEASE_TIMEOUT_SECONDS: Final = 1.0
LEASE_SWEEP_INTERVAL_SECONDS: Final = 600.0
#: A run with no lease file (its producer predates leases, or its free lease was
#: swept) counts as orphaned once it has gone this long without an update. A
#: live service appends a heartbeat every ``DEFAULT_HEARTBEAT_SECONDS``.
ORPHAN_IDLE_SECONDS: Final = 900.0

DEFAULT_HEARTBEAT_SECONDS: Final = 60.0
DEFAULT_DRAIN_SECONDS: Final = 15.0
MINIMUM_HEARTBEAT_SECONDS: Final = 1.0
SOCKET_LISTEN_BACKLOG: Final = 16
SOCKET_ACCEPT_TIMEOUT_SECONDS: Final = 0.5
SOCKET_CONNECTION_TIMEOUT_SECONDS: Final = 0.25
WORKER_INTERVAL_SECONDS: Final = 1.0
DRAIN_RETRY_SECONDS: Final = 0.5

EVENT_OBJECT_ERROR: Final = "event must be an object"
UNSUPPORTED_ACTION_ERROR: Final = "unsupported local telemetry action"
LOCAL_MESSAGE_TOO_LARGE_ERROR: Final = "local telemetry message exceeds 1 MiB"
FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT: Final = "unexpected_process_exit"

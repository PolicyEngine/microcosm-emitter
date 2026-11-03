"""Configuration and protocol values for the telemetry emitter service."""

from __future__ import annotations

from typing import Final

RETENTION_DAYS: Final = 7
MAX_QUEUED_BYTES: Final = 100 * 1024 * 1024
BATCH_SIZE: Final = 100
# The default destination: the collector's own hostname, which a load balancer
# sends to the production Cloud Run service. It stays the same when that service
# moves to another Google Cloud project and its Cloud Run address changes.
PRODUCTION_COLLECTOR_URL: Final = "https://microcosm-collector.policyengine.org"
# The production Cloud Run service's own address, the default before the
# hostname. It reaches the same service and stays trusted, so a module
# configuration's `collector_origin` can still select it. No interface release
# passes that setting today, so rolling back the hostname needs a new emitter
# release.
CLOUD_RUN_COLLECTOR_URL: Final = (
    "https://microcosm-telemetry-389282473430.us-central1.run.app"
)
# The two origins of the one production collector. Collector submission rows
# queued under either are delivered through whichever one an emitter resolves
# (`same_collector_origins`). Both reach the production collector, except that
# while the collector moves to its own Google Cloud project the Cloud Run
# address may reach the old service, which then answers only with maintenance
# responses. Never add an origin that reaches a collector with its own live
# data, such as a staging one: rows queued for it would go to production.
PRODUCTION_COLLECTOR_ORIGIN_PAIR: Final = frozenset(
    {PRODUCTION_COLLECTOR_URL, CLOUD_RUN_COLLECTOR_URL}
)
# The only origins that may receive the builder's Hugging Face token. A module's
# `collector_origin` setting must name one of them, and nothing else (no
# environment variable) chooses the destination. Any origin added here beyond
# the pair is another collector: rows queued for it go only to an emitter that
# resolves that origin.
TRUSTED_COLLECTOR_ORIGINS: Final = PRODUCTION_COLLECTOR_ORIGIN_PAIR

# A collector token obtained on another machine with `microcosm-emitter
# telemetry-token`, for an emitter without a Hugging Face credential, such as
# one in a Modal container. The variables only supply that token:
# MICROCOSM_COLLECTOR_ORIGIN is compared with the origin the emitter resolved,
# and never chooses where it sends. These are the variables' names, not
# secrets.
COLLECTOR_TOKEN_VARIABLE: Final = "MICROCOSM_COLLECTOR_TOKEN"  # noqa: S105
COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE: Final = "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT"  # noqa: S105
COLLECTOR_TOKEN_ORIGIN_VARIABLE: Final = "MICROCOSM_COLLECTOR_ORIGIN"  # noqa: S105

LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
# A URL path, not a secret.
TOKEN_EXCHANGE_PATH: Final = "/v1/auth/huggingface/exchange"  # noqa: S105
TELEMETRY_SCOPE: Final = "telemetry:write"
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
# Why a pending run waits: the collector token this emitter was given has
# expired or was refused (401), and cannot be renewed where it is used. The
# run stays pending, so an emitter with a Hugging Face login delivers it later.
# Stored in the run's `local_only_reason` column, which the spool database
# already has; a successful registration clears it. These are reason codes,
# not secrets.
PENDING_GIVEN_TOKEN_EXPIRED: Final = "collector_token_expired"  # noqa: S105
PENDING_GIVEN_TOKEN_REJECTED: Final = "collector_token_rejected"  # noqa: S105
# Why a pending run waits: the login saved by `hf auth login` could not be read,
# such as a file this user may not read, and neither HF_TOKEN nor
# HUGGINGFACE_TOKEN is set. The login exists and can be repaired, so the run
# stays pending for this emitter, or a later one, to deliver once it can be read.
PENDING_HUGGINGFACE_LOGIN_UNREADABLE: Final = "huggingface_login_unreadable"

NO_CREDENTIAL_MESSAGE: Final = (
    "Microcosm telemetry is local-only: no ambient Hugging Face credential was "
    "found. The dataset build will continue."
)
# Only the error's type, since its text names the file, or quotes its bytes.
UNREADABLE_LOGIN_WAITING_MESSAGE: Final = (
    "Microcosm telemetry is waiting: the Hugging Face login saved by `hf auth "
    "login` could not be read ({error_type}), and neither HF_TOKEN nor "
    "HUGGINGFACE_TOKEN is set. Queued events stay pending, and are delivered once "
    "the login can be read. The dataset build will continue."
)
REJECTED_CREDENTIAL_MESSAGE: Final = (
    "Microcosm telemetry is local-only for this run: the ambient Hugging Face "
    "credential was not accepted as a PolicyEngine organization member. The "
    "dataset build will continue."
)
UNSENDABLE_CREDENTIAL_MESSAGE: Final = (
    "Microcosm telemetry is local-only for this run: the ambient Hugging Face "
    "credential contains characters that cannot be sent as a bearer token, such "
    "as a space or a line break, so it was not sent. Check HF_TOKEN, "
    "HUGGINGFACE_TOKEN or the login saved by `hf auth login`. The dataset build "
    "will continue."
)
GIVEN_TOKEN_UNUSABLE_MESSAGE: Final = (
    # A message, not a secret.
    "Microcosm telemetry is waiting: the collector token this emitter was given "  # noqa: S105
    "has expired or was refused, and it cannot be renewed here. Queued events "
    "stay pending for delivery by an emitter with a Hugging Face login. The "
    "dataset build will continue."
)
COLLECTOR_URL_HTTPS_ERROR: Final = "collector URL must be an HTTPS origin"
COLLECTOR_URL_ORIGIN_ERROR: Final = (
    "collector URL must be an origin without credentials or path data"
)
DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR: Final = (
    "development collector URL must use a loopback address"
)
UNTRUSTED_COLLECTOR_ORIGIN_ERROR: Final = (
    "collector origin is not one of the emitter's trusted collector origins"
)
CONFLICTING_COLLECTOR_ORIGIN_ERROR: Final = (
    "set a development collector URL or a collector origin, not both"
)
COLLECTOR_SETTING_TYPE_ERROR: Final = (
    "a development collector URL or collector origin must be a string"
)
COLLECTOR_RESPONSE_TOO_LARGE_ERROR: Final = "collector response exceeds 1 MiB"
UNSENDABLE_CREDENTIAL_ERROR: Final = (
    "the credential contains characters that cannot be sent as a bearer token"
)
GIVEN_TOKEN_VARIABLES_ERROR: Final = (
    # An error message, not a secret.
    "MICROCOSM_COLLECTOR_TOKEN, MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT and "  # noqa: S105
    "MICROCOSM_COLLECTOR_ORIGIN must be set together"
)
GIVEN_TOKEN_VALUE_ERROR: Final = (
    # An error message, not a secret.
    "MICROCOSM_COLLECTOR_TOKEN must be visible ASCII characters, without spaces "  # noqa: S105
    "or line breaks"
)
GIVEN_TOKEN_EXPIRY_ERROR: Final = (
    # An error message, not a secret.
    "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT must be an ISO 8601 time with a time zone"  # noqa: S105
)
GIVEN_TOKEN_ORIGIN_ERROR: Final = (
    # An error message, not a secret.
    "MICROCOSM_COLLECTOR_ORIGIN is not the collector origin this emitter's module "  # noqa: S105
    "settings resolve (without a `collector_origin` or `development_collector_url` "
    "setting, the origin that `microcosm-emitter version` prints)"
)
# Printed by the telemetry or Orrery module, whichever the host starts first:
# the host itself prints only a fixed startup failure, and exits.
GIVEN_TOKEN_START_ERROR_MESSAGE: Final = (
    # A message, not a secret.
    "The Microcosm emitter cannot start: {reason}. It exits before accepting any "  # noqa: S105
    "request from the build."
)

MINIMUM_TOKEN_LIFETIME_SECONDS: Final = 60
TOKEN_REFRESH_MARGIN_SECONDS: Final = 30
INITIAL_RETRY_SECONDS: Final = 1.0
MAX_RETRY_SECONDS: Final = 60.0

DATABASE_TIMEOUT_SECONDS: Final = 5
PRUNE_INTERVAL_SECONDS: Final = 60.0

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

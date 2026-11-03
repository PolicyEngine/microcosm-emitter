"""The HTTP status classes the emitter's collector clients share."""

from http import HTTPStatus
from typing import Final

#: Client errors that a later attempt need not repeat: a timeout and rate
#: limiting.
RETRYABLE_CLIENT_ERRORS: Final = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)


def is_permanent_client_error(status: int) -> bool:
    """Return whether ``status`` is a client error (4xx) a retry would repeat.

    Every 4xx status counts except those in ``RETRYABLE_CLIENT_ERRORS``.
    """
    return (
        HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
        and status not in RETRYABLE_CLIENT_ERRORS
    )

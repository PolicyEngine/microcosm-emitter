"""The `microcosm-emitter` command installed with `uv tool install microcosm-emitter`.

`microcosm-emitter host` runs the local service host. A build starts it as its
own child process, so the host inherits the build's environment variables and
user, including any Hugging Face credential. It is never a system service.

`microcosm-emitter telemetry-token` runs on the operator's workstation when it
launches a build on a machine without a Hugging Face credential, such as
Modal: it obtains a collector token that can only write telemetry, for the
emitter on that machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TYPE_CHECKING, Final

from microcosm_emitter.compatibility import (
    SUPPORTED_PROTOCOL_VERSIONS,
    EmitterDescription,
    VersionReport,
)
from microcosm_emitter.core.constants import TELEMETRY_SCOPE

if TYPE_CHECKING:
    from microcosm_emitter.core.auth import ExchangeFailureResult

DISTRIBUTION_NAME = "microcosm-emitter"

#: Exit codes of `telemetry-token`, one per kind of failure; 2 is a usage
#: error, as for every command, and 7 an unexpected error, which the others do
#: not name. A failed OIDC token exchange while HF_OIDC_RESOURCE is set is 7:
#: it fails at the CI provider or the Hub before the collector is contacted,
#: so it is not 5, and it is not a missing or unreadable login, so not 3.
EXIT_MISSING_CREDENTIAL: Final = 3
EXIT_REJECTED_CREDENTIAL: Final = 4
EXIT_COLLECTOR_UNAVAILABLE: Final = 5
EXIT_LIFETIME_UNSUPPORTED: Final = 6
EXIT_UNEXPECTED_FAILURE: Final = 7
#: Each exchange that gave no token: its exit code, and one line for standard
#: error. The scope is `telemetry:write`, so ``intake_unavailable`` does not
#: occur. An unsendable credential exits as a refused one: it is not a bearer
#: token the collector would accept.
TELEMETRY_TOKEN_FAILURES: Final[dict[ExchangeFailureResult, tuple[int, str]]] = {
    "missing": (
        EXIT_MISSING_CREDENTIAL,
        "No Hugging Face credential was found: set HF_TOKEN or HUGGINGFACE_TOKEN, "
        "or run `hf auth login`.",
    ),
    "unsendable": (
        EXIT_REJECTED_CREDENTIAL,
        "The Hugging Face credential was not sent: it contains characters that "
        "cannot be sent as a bearer token, such as a space or a line break. Check "
        "HF_TOKEN, HUGGINGFACE_TOKEN or the login saved by `hf auth login`.",
    ),
    "rejected": (
        EXIT_REJECTED_CREDENTIAL,
        "The collector refused the Hugging Face credential ({answer}): it issues "
        "tokens only to members of the PolicyEngine organization.",
    ),
    "unavailable": (
        EXIT_COLLECTOR_UNAVAILABLE,
        "The collector could not be reached or gave no usable token ({answer}).",
    ),
    "lifetime_unsupported": (
        EXIT_LIFETIME_UNSUPPORTED,
        "The collector refused the request ({answer}): it does not accept "
        "`lifetime_seconds`, so it cannot issue a token for another machine yet.",
    ),
}
#: `huggingface_hub` could not read the login `hf auth login` saved, such as a
#: file this user may not read, and neither variable is set
#: (``UnreadableLoginError``).
UNREADABLE_LOGIN_MESSAGE: Final = (
    "The Hugging Face login saved by `hf auth login` could not be read "
    "({error_type}), and neither HF_TOKEN nor HUGGINGFACE_TOKEN is set; nothing "
    "was sent."
)
#: Only the error's type: its text can quote the credential, as http.client's
#: error for a header it cannot send does.
TELEMETRY_TOKEN_UNEXPECTED_FAILURE: Final = (
    "No telemetry token was obtained: an unexpected {error_type} occurred. Its "
    "details are not printed, because they can contain the credential."
)


def describe() -> EmitterDescription:
    """Versions a client checks before sending any hand-over."""
    return {
        "emitter_version": version(DISTRIBUTION_NAME),
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
    }


def version_report() -> VersionReport:
    """Return the emitter's versions and the collector origin it sends to by default."""
    # Imported only when this runs, so `microcosm-emitter host` never loads the
    # collector code.
    from microcosm_emitter.core.collector_origin import (  # noqa: PLC0415
        resolve_collector_origin,
    )

    return {**describe(), "collector_origin": resolve_collector_origin()}


def _positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        error_message = "must be a positive whole number"
        raise argparse.ArgumentTypeError(error_message)
    return number


def _collector_origin(development_collector_url: str | None) -> str:
    # Imported only when this runs, so `microcosm-emitter host` never loads the
    # collector code.
    from microcosm_emitter.core.collector_origin import (  # noqa: PLC0415
        resolve_collector_origin,
    )

    try:
        return resolve_collector_origin(
            development_collector_url=development_collector_url
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _add_development_collector_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--development-collector-url",
        dest="collector_origin",
        metavar="URL",
        type=_collector_origin,
        default=None,
        help="A loopback collector, for tests and local development. Without "
        "it, the collector origin that `version` prints.",
    )


def telemetry_token(arguments: argparse.Namespace) -> int:
    """Print a telemetry token, its expiry and the collector origin as JSON.

    The token goes to standard output only. A failure prints one line on
    standard error, without the token, the Hugging Face credential or the
    collector's answer body; ``main`` turns any other error into exit 7.
    """
    # Imported only when the command runs, so `microcosm-emitter host` never loads
    # it, and looked up then, so the tests' patches of core.auth apply.
    from microcosm_emitter.core.auth import (  # noqa: PLC0415
        ExchangeFailure,
        UnreadableLoginError,
        exchange_huggingface_credential,
        huggingface_token,
    )

    origin = arguments.collector_origin or _collector_origin(None)
    try:
        credential = huggingface_token()
    except UnreadableLoginError as error:
        print(
            UNREADABLE_LOGIN_MESSAGE.format(error_type=error.error_type),
            file=sys.stderr,
        )
        return EXIT_MISSING_CREDENTIAL
    # Counted from before the request, so it never overstates the expiry.
    requested_at = time.time()
    exchange = exchange_huggingface_credential(
        origin,
        credential,
        scope=TELEMETRY_SCOPE,
        lifetime_seconds=arguments.lifetime_seconds,
    )
    if isinstance(exchange, ExchangeFailure):
        exit_code, message = TELEMETRY_TOKEN_FAILURES[exchange.result]
        answer = "no answer" if exchange.status is None else f"HTTP {exchange.status}"
        print(message.format(answer=answer), file=sys.stderr)
        return exit_code
    expires_at = datetime.fromtimestamp(requested_at + exchange.expires_in, UTC)
    print(
        json.dumps(
            {
                "access_token": exchange.access_token,
                "expires_at": expires_at.isoformat(timespec="seconds"),
                "collector_origin": origin,
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one `microcosm-emitter` command and return its exit code.

    ``argv`` defaults to ``sys.argv[1:]``. ``host`` hands the remaining arguments
    to the host's own parser; ``version`` prints ``version_report()`` as JSON;
    and an unexpected error in ``telemetry-token`` exits 7, naming only the
    error's type.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="microcosm-emitter", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "host",
        help="Run the local service host; its configuration arrives on stdin.",
        add_help=False,
    )
    commands.add_parser(
        "version",
        help="Print the emitter and socket protocol versions and the collector "
        "origin as JSON.",
    )
    token_parser = commands.add_parser(
        "telemetry-token",
        help="Exchange this machine's Hugging Face login for a collector token "
        "that only writes telemetry, for a build on a machine without one. "
        "Prints the token, its expiry and the collector origin as JSON.",
    )
    token_parser.add_argument(
        "--lifetime-seconds",
        type=_positive_integer,
        required=True,
        help="How long the token should last; the collector may grant less.",
    )
    _add_development_collector_url(token_parser)
    if arguments[:1] == ["host"]:
        # The host parses its own options.
        from microcosm_emitter.host.__main__ import main as host_main

        return host_main(arguments[1:], description=describe())
    parsed = parser.parse_args(arguments)
    if parsed.command == "telemetry-token":
        try:
            return telemetry_token(parsed)
        except Exception as error:
            print(
                TELEMETRY_TOKEN_UNEXPECTED_FAILURE.format(
                    error_type=type(error).__name__
                ),
                file=sys.stderr,
            )
            return EXIT_UNEXPECTED_FAILURE
    print(json.dumps(version_report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

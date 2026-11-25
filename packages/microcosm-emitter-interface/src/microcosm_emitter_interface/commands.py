"""Run the installed emitter's ``version``, ``status`` and ``telemetry-token``.

Each call runs its command once, as this process's child with standard input
closed, and returns the parsed answer or a ``CommandFailure`` that says why
there is none. No call raises into the build: not when the command is
missing, cannot start, is slow, fails or prints something else, and not when
an argument is unusable. The command is the one ``launcher.resolve_command``
finds: the ``command`` argument, then ``MICROCOSM_EMITTER_COMMAND``, then
``microcosm-emitter`` on ``PATH``, all read from this process's environment.
``environment`` is the whole environment the command runs with; without it,
the command inherits this process's.

Each answer has a field for every key the emitter's README documents for its
command and ignores any other key, so an emitter that prints more still
parses. ``fixtures/commands/<command>.json`` records each command's output as
documented; the emitter's CI checks that its commands print exactly those
keys and that these calls parse them. ``recorded_command_output`` returns one,
for tests that stand in for the emitter.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from typing import Final, Literal
from urllib.parse import urlsplit

from microcosm_emitter_interface.launcher import (
    EmitterStartupError,
    resolve_command,
    start_command_process,
)
from microcosm_emitter_interface.protocol import STARTUP_TIMEOUT_SECONDS

type CommandName = Literal["version", "status", "telemetry-token"]

type CommandFailureReason = Literal[
    # An argument of the call is unusable; nothing was run.
    "invalid_arguments",
    # No command was found (``launcher.resolve_command``).
    "command_not_found",
    # The command was found but could not be started; see ``error_type``.
    "could_not_run",
    # The command did not exit within the timeout, so it was stopped.
    "timed_out",
    # Exit 2, argparse's usage error, which every command shares; an emitter
    # without the command (``status`` before 0.13.0, ``telemetry-token``
    # before 0.11.0, ``version`` before 0.4.0) exits 2 too.
    "usage_error",
    # ``telemetry-token`` exits 3 to 7.
    "missing_credential",
    "rejected_credential",
    "collector_unavailable",
    "lifetime_unsupported",
    "unexpected_failure",
    # ``status`` exits 8 and 9.
    "no_event_queue_file",
    "unusable_event_queue_file",
    # An exit code the command does not document, or a signal (negative).
    "exited",
    # Exit 0, but the output is not the documented answer; see
    # ``output_problem``.
    "unusable_output",
]

COMMAND_NAMES: Final[tuple[CommandName, ...]] = ("version", "status", "telemetry-token")

#: Exit codes the commands document, as the emitter's README lists them; the
#: emitter's ``cli`` reads them from here. 2 is argparse's usage error.
EXIT_USAGE_ERROR: Final = 2
EXIT_MISSING_CREDENTIAL: Final = 3
EXIT_REJECTED_CREDENTIAL: Final = 4
EXIT_COLLECTOR_UNAVAILABLE: Final = 5
EXIT_LIFETIME_UNSUPPORTED: Final = 6
EXIT_UNEXPECTED_FAILURE: Final = 7
EXIT_NO_EVENT_QUEUE_FILE: Final = 8
EXIT_UNUSABLE_EVENT_QUEUE_FILE: Final = 9

#: How long each call waits by default. ``version`` starts no module, so it
#: has the emitter's startup time; ``status`` waits at most the emitter's 5 s
#: busy timeout for another connection's commit; ``telemetry-token`` makes
#: one exchange with the collector.
VERSION_TIMEOUT_SECONDS: Final = STARTUP_TIMEOUT_SECONDS
STATUS_TIMEOUT_SECONDS: Final = 60.0
TELEMETRY_TOKEN_TIMEOUT_SECONDS: Final = 120.0
#: The longest ``CommandFailure.message``, which ends in an ellipsis if cut.
MAX_MESSAGE_CHARS: Final = 300

COMMAND_FIXTURE_DIRECTORY: Final = "commands"

#: What ``CommandFailure.output_problem`` says about unusable output. None
#: quotes the output.
NOT_A_JSON_OBJECT: Final = "it printed no JSON object"
NO_LIST_OF_RUNS: Final = (
    "it printed no list of runs, each with a text run_id, producer_id and "
    "upload_state, a text or null reason and a count undelivered_events of 0 "
    "or more"
)
NO_SENDABLE_ACCESS_VALUE: Final = (
    "it printed no access_token of visible ASCII characters"
)
NO_EXPIRY_WITH_TIME_ZONE: Final = (
    "its expires_at is not an ISO 8601 time with a time zone"
)
NO_COLLECTOR_ORIGIN: Final = "its collector_origin is not an HTTPS origin"

_EXIT_REASONS: Final[Mapping[CommandName, Mapping[int, CommandFailureReason]]] = {
    "version": {EXIT_USAGE_ERROR: "usage_error"},
    "status": {
        EXIT_USAGE_ERROR: "usage_error",
        EXIT_NO_EVENT_QUEUE_FILE: "no_event_queue_file",
        EXIT_UNUSABLE_EVENT_QUEUE_FILE: "unusable_event_queue_file",
    },
    "telemetry-token": {
        EXIT_USAGE_ERROR: "usage_error",
        EXIT_MISSING_CREDENTIAL: "missing_credential",
        EXIT_REJECTED_CREDENTIAL: "rejected_credential",
        EXIT_COLLECTOR_UNAVAILABLE: "collector_unavailable",
        EXIT_LIFETIME_UNSUPPORTED: "lifetime_unsupported",
        EXIT_UNEXPECTED_FAILURE: "unexpected_failure",
    },
}
#: Visible ASCII, the only characters a bearer token or an origin may hold.
_VISIBLE_ASCII: Final = re.compile(r"[!-~]+")
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_STATUS_TEXT_FIELDS: Final = ("run_id", "producer_id", "upload_state")


@dataclass(frozen=True)
class CommandFailure:
    """Why a command call has no answer.

    ``exit_code`` is the command's exit code, or None when it did not exit
    on its own. ``message`` is the last line the command wrote on standard
    error, with its whitespace collapsed, at most ``MAX_MESSAGE_CHARS``
    characters, or None without one; it never comes from standard output,
    and a ``telemetry-token`` that printed anything on standard output gets
    none. ``error_type`` names the exception that kept a found command from
    starting (``could_not_run``), never its text. ``output_problem`` says
    what was wrong with the output (``unusable_output``) without quoting it.
    """

    command: CommandName
    reason: CommandFailureReason
    exit_code: int | None = None
    message: str | None = None
    error_type: str | None = None
    output_problem: str | None = None


@dataclass(frozen=True)
class EmitterVersion:
    """What ``microcosm-emitter version`` reports.

    Each field is None when the printed object lacks it or holds another
    type, so that a caller can still name an emitter too old for it:
    ``collector_origin`` is printed from 0.11.0. ``protocol_versions`` holds
    the socket protocol versions the emitter accepts.
    """

    emitter_version: str | None
    protocol_versions: tuple[int, ...] | None
    collector_origin: str | None


@dataclass(frozen=True)
class EventQueueRun:
    """One run in an emitter's event queue file, as ``status`` prints it.

    ``reason`` is the run's ``local_only_reason``: why a ``local_only`` run
    is excluded from upload, or why a ``pending`` one waits.
    ``undelivered_events`` counts the run's events the file still holds; the
    emitter also prunes events it never delivered, so 0 means none are
    held, not that every event was delivered.
    """

    run_id: str
    producer_id: str
    upload_state: str
    reason: str | None
    undelivered_events: int


@dataclass(frozen=True)
class EmitterStatus:
    """Every run ``microcosm-emitter status`` printed, in its order."""

    runs: tuple[EventQueueRun, ...]


@dataclass(frozen=True)
class TelemetryToken:
    """A collector token that only writes telemetry, for a build elsewhere.

    ``expires_at`` is the ISO 8601 time, with its time zone, as
    ``telemetry-token`` printed it; ``collector_origin`` is the collector it
    was obtained from, without a trailing slash. The repr leaves the token
    out.
    """

    access_token: str = field(repr=False)
    expires_at: str
    collector_origin: str

    @property
    def expiry(self) -> datetime:
        """``expires_at`` as a time with its time zone."""
        return datetime.fromisoformat(self.expires_at)


@dataclass(frozen=True)
class _Output:
    """What a command that exited printed, decoded as UTF-8 with replacement."""

    exit_code: int
    stdout: str
    stderr: str


def emitter_version(
    *,
    command: str | os.PathLike[str] | None = None,
    timeout: float = VERSION_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> EmitterVersion | CommandFailure:
    """Run ``microcosm-emitter version``; return what it reports, or why not.

    Any JSON object is an answer (see ``EmitterVersion``); output that is not
    one is ``unusable_output``.
    """
    output = _run("version", ["version"], command, timeout, environment)
    if isinstance(output, CommandFailure):
        return output
    answer = _json_object(output.stdout)
    if answer is None:
        return _unusable("version", NOT_A_JSON_OBJECT)
    reported = answer.get("emitter_version")
    versions = answer.get("protocol_versions")
    origin = answer.get("collector_origin")
    return EmitterVersion(
        emitter_version=reported if isinstance(reported, str) else None,
        protocol_versions=(
            tuple(versions)
            if isinstance(versions, list)
            and all(type(version) is int for version in versions)
            else None
        ),
        collector_origin=origin if isinstance(origin, str) and origin else None,
    )


def emitter_status(
    *,
    event_queue_file: str | os.PathLike[str] | None = None,
    command: str | os.PathLike[str] | None = None,
    timeout: float = STATUS_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> EmitterStatus | CommandFailure:
    """Run ``microcosm-emitter status``; return every run it printed, or why not.

    Without ``event_queue_file`` the command reads the default path for the
    environment it runs in, which is a build's file only when the build
    passed its emitter no path and ran with the same environment. No file at
    the path is ``no_event_queue_file`` (exit 8), and a file that cannot be
    read as an event queue file ``unusable_event_queue_file`` (exit 9); each
    ``message`` names the path.
    """
    arguments = ["status"]
    if event_queue_file is not None:
        path = _path_text(event_queue_file)
        if path is None:
            return CommandFailure("status", "invalid_arguments")
        arguments.append(f"--event-queue-file={path}")
    output = _run("status", arguments, command, timeout, environment)
    if isinstance(output, CommandFailure):
        return output
    answer = _json_object(output.stdout)
    printed_runs = None if answer is None else answer.get("runs")
    if not isinstance(printed_runs, list):
        return _unusable("status", NO_LIST_OF_RUNS)
    runs = [_event_queue_run(run) for run in printed_runs]
    parsed = [run for run in runs if run is not None]
    if len(parsed) != len(runs):
        return _unusable("status", NO_LIST_OF_RUNS)
    return EmitterStatus(tuple(parsed))


def telemetry_token(
    lifetime_seconds: int,
    *,
    development_collector_url: str | None = None,
    command: str | os.PathLike[str] | None = None,
    timeout: float = TELEMETRY_TOKEN_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> TelemetryToken | CommandFailure:
    """Run ``microcosm-emitter telemetry-token``; return the token, or why not.

    The command exchanges the Hugging Face credential of the environment it
    runs in for a token that only writes telemetry and lasts
    ``lifetime_seconds`` (a whole number of 1 or more), or as long as the
    collector allows: compare ``expiry`` with what was asked for.
    ``development_collector_url`` asks a loopback collector instead, for tests;
    only then may the answer's origin be a loopback HTTP one. Exits 2 to 7
    are their own reasons. The token is read from standard output only: no
    failure holds it, and the answer's repr leaves it out.
    """
    if (
        type(lifetime_seconds) is not int
        or lifetime_seconds < 1
        or (
            development_collector_url is not None
            and (
                not isinstance(development_collector_url, str)
                or not development_collector_url
            )
        )
    ):
        return CommandFailure("telemetry-token", "invalid_arguments")
    arguments = ["telemetry-token", f"--lifetime-seconds={lifetime_seconds}"]
    if development_collector_url is not None:
        arguments.append(f"--development-collector-url={development_collector_url}")
    output = _run("telemetry-token", arguments, command, timeout, environment)
    if isinstance(output, CommandFailure):
        return output
    return _token_answer(
        output.stdout, loopback_allowed=development_collector_url is not None
    )


def recorded_command_output(command: CommandName) -> str:
    """Return what ``command`` prints on success as recorded: one JSON line.

    The line ends in a line break, as the command's own output does. An
    unknown command raises ``ValueError``.
    """
    if command not in COMMAND_NAMES:
        error_message = "Unknown emitter command."
        raise ValueError(error_message)
    recorded = (
        files("microcosm_emitter_interface")
        .joinpath("fixtures", COMMAND_FIXTURE_DIRECTORY, f"{command}.json")
        .read_text(encoding="utf-8")
    )
    return json.dumps(json.loads(recorded)) + "\n"


def _run(
    name: CommandName,
    arguments: list[str],
    command: str | os.PathLike[str] | None,
    timeout: float,
    environment: Mapping[str, str] | None,
) -> _Output | CommandFailure:
    """Run the command to its exit; a failure for anything but exit 0."""
    if not _usable_timeout(timeout) or not _usable_environment(environment):
        return CommandFailure(name, "invalid_arguments")
    try:
        executable = resolve_command(command)
    # shutil.which raises ValueError for a name with a null character.
    except (EmitterStartupError, ValueError, TypeError, OSError):
        return CommandFailure(name, "command_not_found")
    try:
        process = start_command_process(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            environment=environment,
        )
    # Popen raises ValueError for an environment name with "=" or a null
    # character, and OSError when the found command cannot be executed.
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return CommandFailure(name, "could_not_run", error_type=type(error).__name__)
    with process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return CommandFailure(name, "timed_out")
        except BaseException:
            # As subprocess.run does: never leave the command running.
            process.kill()
            raise
    output = _Output(
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
    if output.exit_code == 0:
        return output
    message = _message(output.stderr)
    if name == "telemetry-token" and output.stdout.strip():
        # Standard output may hold the token, so nothing of it is passed on.
        message = None
    return CommandFailure(
        name,
        _EXIT_REASONS[name].get(output.exit_code, "exited"),
        exit_code=output.exit_code,
        message=message,
    )


def _usable_timeout(timeout: object) -> bool:
    return (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and timeout > 0
    )


def _usable_environment(environment: object) -> bool:
    return environment is None or (
        isinstance(environment, Mapping)
        and all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in environment.items()
        )
    )


def _path_text(path: str | os.PathLike[str]) -> str | None:
    """``path`` as text, or None if it is not a non-empty text path."""
    try:
        text = os.fspath(path)
    # Any other object, such as a number, is no path.
    except TypeError:
        return None
    return text if isinstance(text, str) and text else None


def _message(stderr: str) -> str | None:
    """Return the last non-blank line of ``stderr``, collapsed and shortened."""
    lines = [" ".join(line.split()) for line in stderr.splitlines()]
    last = next((line for line in reversed(lines) if line), None)
    if last is None or len(last) <= MAX_MESSAGE_CHARS:
        return last
    return last[: MAX_MESSAGE_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _json_object(stdout: str) -> dict[str, object] | None:
    try:
        value = json.loads(stdout)
    # Output nested deeper than the parser recurses raises RecursionError.
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _unusable(name: CommandName, problem: str) -> CommandFailure:
    return CommandFailure(name, "unusable_output", exit_code=0, output_problem=problem)


def _event_queue_run(run: object) -> EventQueueRun | None:
    """One printed run, or None unless it has every documented field."""
    if not isinstance(run, dict) or "reason" not in run:
        return None
    texts = [run.get(name) for name in _STATUS_TEXT_FIELDS]
    reason = run["reason"]
    count = run.get("undelivered_events")
    if (
        not all(isinstance(text, str) for text in texts)
        or (reason is not None and not isinstance(reason, str))
        or type(count) is not int
        or count < 0
    ):
        return None
    run_id, producer_id, upload_state = (str(text) for text in texts)
    return EventQueueRun(run_id, producer_id, upload_state, reason, count)


def _token_answer(
    stdout: str, *, loopback_allowed: bool
) -> TelemetryToken | CommandFailure:
    """Return the token ``telemetry-token`` printed, or why it is unusable."""
    answer = _json_object(stdout)
    if answer is None:
        return _unusable("telemetry-token", NOT_A_JSON_OBJECT)
    access_token = answer.get("access_token")
    expires_at = answer.get("expires_at")
    origin = _collector_origin(
        answer.get("collector_origin"), loopback_allowed=loopback_allowed
    )
    if not isinstance(access_token, str) or not _VISIBLE_ASCII.fullmatch(access_token):
        return _unusable("telemetry-token", NO_SENDABLE_ACCESS_VALUE)
    if not isinstance(expires_at, str) or not _has_time_zone(expires_at):
        return _unusable("telemetry-token", NO_EXPIRY_WITH_TIME_ZONE)
    if origin is None:
        return _unusable("telemetry-token", NO_COLLECTOR_ORIGIN)
    return TelemetryToken(access_token, expires_at, origin)


def _has_time_zone(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _collector_origin(value: object, *, loopback_allowed: bool) -> str | None:
    """``value`` without a trailing slash if it is a usable origin, else None.

    An HTTPS origin: a host, no user, path, query or fragment. With
    ``loopback_allowed``, an HTTP origin on a loopback host too.
    """
    if not isinstance(value, str) or not _VISIBLE_ASCII.fullmatch(value):
        return None
    try:
        parts = urlsplit(value)
        # Reading the port refuses one that is not a number in range.
        _ = parts.port
    except ValueError:
        return None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        return None
    if parts.scheme == "https" or (
        loopback_allowed
        and parts.scheme == "http"
        and parts.hostname in _LOOPBACK_HOSTS
    ):
        return value.removesuffix("/")
    return None

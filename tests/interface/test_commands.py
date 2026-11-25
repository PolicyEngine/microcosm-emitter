"""Typed calls for the emitter's `version`, `status` and `telemetry-token` commands.

Each test runs a stand-in for the installed command that records its
arguments and prints what the test chooses, so every documented exit code and
every kind of unusable output is covered without a collector or an event
queue file. `test_command_contracts.py` runs the real installed command.
"""

import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from microcosm_emitter_interface import commands
from microcosm_emitter_interface.commands import (
    CommandFailure,
    EmitterStatus,
    EmitterVersion,
    EventQueueRun,
    TelemetryToken,
    emitter_status,
    emitter_version,
    telemetry_token,
)
from microcosm_emitter_interface.launcher import (
    COMMAND_ENVIRONMENT_VARIABLE,
    DEFAULT_COMMAND,
)

#: A token only these tests print; no error text or repr may contain it.
STAND_IN_TOKEN = "collector-token-that-must-stay-private"
TOKEN_LIFETIME_SECONDS = 18 * 3600
DOCUMENTED_TOKEN = {
    "access_token": STAND_IN_TOKEN,
    "expires_at": "2026-11-27T06:00:00+00:00",
    "collector_origin": "https://collector.example",
}
DOCUMENTED_RUNS = [
    {
        "run_id": "finished-run",
        "producer_id": "modal",
        "upload_state": "pending",
        "reason": None,
        "undelivered_events": 0,
    },
    {
        "run_id": "refused-run",
        "producer_id": "modal",
        "upload_state": "local_only",
        "reason": "run_registration_rejected",
        "undelivered_events": 1,
    },
]
USAGE_ERROR_TEXT = (
    "usage: microcosm-emitter [-h] {host,version} ...\n"
    "microcosm-emitter: error: argument command: invalid choice: 'status'\n"
)


@pytest.fixture
def stand_in(emitter_command_script, tmp_path):
    """Write a stand-in emitter command; return a function that makes one.

    The stand-in records its arguments and the ``COMMAND_PROBE`` variable it
    sees in ``tmp_path / "calls.jsonl"``, waits ``sleep_seconds``, writes
    ``stdout`` and ``stderr`` and exits with ``exit_code``.
    """
    calls = tmp_path / "calls.jsonl"

    def make(
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        sleep_seconds: float = 0,
        name: str = "stand-in-emitter",
    ) -> Path:
        source = f"""
import json, os, sys, time
with open({str(calls)!r}, "a") as calls:
    calls.write(json.dumps({{
        "arguments": sys.argv[1:],
        "probe": os.environ.get("COMMAND_PROBE"),
    }}) + "\\n")
time.sleep({sleep_seconds!r})
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit({exit_code!r})
"""
        return emitter_command_script(name, source)

    return make


@pytest.fixture
def recorded_calls(tmp_path):
    """Return each recorded stand-in call so far, oldest first."""

    def read() -> list[dict]:
        path = tmp_path / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    return read


def printed(document: object) -> str:
    return json.dumps(document) + "\n"


# --------------------------------------------------------------------------- #
# Finding and running the command                                             #
# --------------------------------------------------------------------------- #


def test_each_call_runs_its_command_with_its_documented_arguments(
    stand_in, recorded_calls, tmp_path
):
    command = stand_in(stdout=printed({"emitter_version": "0.17.0"}))
    queue_file = tmp_path / "queue" / "events.sqlite3"

    emitter_version(command=command)
    emitter_status(command=command)
    emitter_status(command=command, event_queue_file=queue_file)
    telemetry_token(TOKEN_LIFETIME_SECONDS, command=command)
    telemetry_token(
        3600, command=command, development_collector_url="http://127.0.0.1:9"
    )

    assert [call["arguments"] for call in recorded_calls()] == [
        ["version"],
        ["status"],
        ["status", f"--event-queue-file={queue_file}"],
        ["telemetry-token", f"--lifetime-seconds={TOKEN_LIFETIME_SECONDS}"],
        [
            "telemetry-token",
            "--lifetime-seconds=3600",
            "--development-collector-url=http://127.0.0.1:9",
        ],
    ]


def test_the_environment_variable_names_the_command_when_no_argument_does(
    stand_in, recorded_calls, isolated_path, monkeypatch
):
    configured = stand_in(stdout=printed({"emitter_version": "0.17.0"}))
    monkeypatch.setenv(COMMAND_ENVIRONMENT_VARIABLE, str(configured))

    answer = emitter_version()

    assert answer == EmitterVersion("0.17.0", None, None)
    assert len(recorded_calls()) == 1


def test_the_command_on_the_path_is_the_default(
    stand_in, recorded_calls, isolated_path, monkeypatch
):
    monkeypatch.delenv(COMMAND_ENVIRONMENT_VARIABLE, raising=False)
    shutil.copy(
        stand_in(stdout=printed({"emitter_version": "0.17.0"})),
        isolated_path / DEFAULT_COMMAND,
    )

    answer = emitter_version()

    assert answer == EmitterVersion("0.17.0", None, None)
    assert len(recorded_calls()) == 1


@pytest.mark.parametrize("call", ["version", "status", "telemetry-token"])
def test_a_missing_command_is_a_failure_not_an_error(isolated_path, monkeypatch, call):
    monkeypatch.delenv(COMMAND_ENVIRONMENT_VARIABLE, raising=False)

    answer = run_call(call)

    assert answer == CommandFailure(call, "command_not_found")


def test_a_command_that_cannot_start_is_a_failure_naming_the_error_type(
    emitter_command_script,
):
    # An executable whose interpreter does not exist: found, but not runnable.
    unrunnable = emitter_command_script("unrunnable", "")
    unrunnable.write_text("#!/nonexistent/interpreter\n")

    answer = emitter_version(command=unrunnable)

    assert isinstance(answer, CommandFailure)
    assert answer.reason == "could_not_run"
    assert answer.error_type is not None
    assert answer.exit_code is None


@pytest.mark.parametrize("call", ["version", "status", "telemetry-token"])
def test_a_command_that_does_not_answer_in_time_is_stopped(stand_in, call):
    slow = stand_in(stdout=printed({}), sleep_seconds=30)
    started = time.monotonic()

    answer = run_call(call, command=slow, timeout=0.5)

    assert answer == CommandFailure(call, "timed_out")
    assert time.monotonic() - started < 10


def test_the_command_inherits_this_process_environment_without_one(
    stand_in, recorded_calls, monkeypatch
):
    monkeypatch.setenv("COMMAND_PROBE", "inherited")
    command = stand_in(stdout=printed({}))

    emitter_version(command=command)
    emitter_version(
        command=command,
        environment={"PATH": os.environ["PATH"], "COMMAND_PROBE": "given"},
    )
    emitter_version(command=command, environment={"PATH": os.environ["PATH"]})

    assert [call["probe"] for call in recorded_calls()] == [
        "inherited",
        "given",
        None,
    ]


@pytest.mark.parametrize(
    ("call", "options"),
    [
        ("version", {"timeout": 0}),
        ("version", {"timeout": -1}),
        ("version", {"timeout": float("nan")}),
        ("version", {"timeout": float("inf")}),
        ("version", {"timeout": "15"}),
        ("version", {"timeout": True}),
        ("version", {"environment": {"PATH": 1}}),
        ("version", {"environment": ["PATH"]}),
        ("status", {"event_queue_file": 5}),
        ("status", {"event_queue_file": ""}),
        ("telemetry-token", {"lifetime_seconds": 0}),
        ("telemetry-token", {"lifetime_seconds": -1}),
        ("telemetry-token", {"lifetime_seconds": True}),
        ("telemetry-token", {"lifetime_seconds": 1.5}),
        ("telemetry-token", {"lifetime_seconds": "3600"}),
        ("telemetry-token", {"development_collector_url": 5}),
        ("telemetry-token", {"development_collector_url": ""}),
    ],
)
def test_unusable_arguments_are_a_failure_and_run_nothing(
    stand_in, recorded_calls, call, options
):
    command = stand_in(stdout=printed({}))

    answer = run_call(call, command=command, **options)

    assert answer == CommandFailure(call, "invalid_arguments")
    assert recorded_calls() == []


def run_call(call, **options):
    """Run ``call`` with ``options``; the token's lifetime defaults to an hour."""
    if call == "version":
        return emitter_version(**options)
    if call == "status":
        return emitter_status(**options)
    lifetime_seconds = options.pop("lifetime_seconds", 3600)
    return telemetry_token(lifetime_seconds, **options)


# --------------------------------------------------------------------------- #
# version                                                                      #
# --------------------------------------------------------------------------- #


def test_version_returns_the_documented_answer(stand_in):
    command = stand_in(
        stdout=printed(
            {
                "emitter_version": "0.17.0",
                "protocol_versions": [1],
                "collector_origin": "https://collector.example",
            }
        )
    )

    answer = emitter_version(command=command)

    assert answer == EmitterVersion("0.17.0", (1,), "https://collector.example")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        # Releases before 0.11.0 print no collector origin.
        (
            {"emitter_version": "0.5.1", "protocol_versions": [1]},
            EmitterVersion("0.5.1", (1,), None),
        ),
        ({}, EmitterVersion(None, None, None)),
        (
            {
                "emitter_version": 17,
                "protocol_versions": ["1"],
                "collector_origin": 5,
            },
            EmitterVersion(None, None, None),
        ),
        (
            {"protocol_versions": [True], "collector_origin": ""},
            EmitterVersion(None, None, None),
        ),
        (
            {"emitter_version": "0.17.0", "protocol_versions": 1},
            EmitterVersion("0.17.0", None, None),
        ),
        # A key a later release adds is ignored.
        (
            {"emitter_version": "1.0.0", "protocol_versions": [1, 2], "new": True},
            EmitterVersion("1.0.0", (1, 2), None),
        ),
    ],
    ids=[
        "older release",
        "empty object",
        "every field of another type",
        "boolean protocol version and empty origin",
        "protocol versions not a list",
        "undocumented key",
    ],
)
def test_version_reads_any_object_so_an_outdated_emitter_can_be_named(
    stand_in, document, expected
):
    answer = emitter_version(command=stand_in(stdout=printed(document)))

    assert answer == expected


@pytest.mark.parametrize(
    "stdout",
    ["", "not json\n", printed([1]), printed("0.17.0"), "[" * 100_000],
    ids=["nothing", "not JSON", "a list", "a string", "nested too deeply"],
)
def test_version_output_that_is_not_a_json_object_is_unusable(stand_in, stdout):
    answer = emitter_version(command=stand_in(stdout=stdout))

    assert isinstance(answer, CommandFailure)
    assert (answer.command, answer.reason, answer.exit_code) == (
        "version",
        "unusable_output",
        0,
    )
    assert answer.output_problem


def test_version_exit_2_is_a_usage_error_with_the_last_line_of_its_message(
    stand_in,
):
    # What a release without the command prints.
    answer = emitter_version(command=stand_in(stderr=USAGE_ERROR_TEXT, exit_code=2))

    assert answer == CommandFailure(
        "version",
        "usage_error",
        exit_code=2,
        message="microcosm-emitter: error: argument command: invalid choice: 'status'",
    )


def test_version_exit_code_it_does_not_document_is_exited(stand_in):
    answer = emitter_version(
        command=stand_in(
            stderr="Traceback (most recent call last):\n  ...\nImportError: x\n\n",
            exit_code=1,
        )
    )

    assert answer == CommandFailure(
        "version", "exited", exit_code=1, message="ImportError: x"
    )


def test_a_failure_message_is_one_line_of_at_most_300_characters(stand_in):
    answer = emitter_version(
        command=stand_in(stderr="first\n  " + "word  " * 200 + "\n", exit_code=1)
    )

    assert isinstance(answer, CommandFailure)
    assert answer.message is not None
    assert len(answer.message) == commands.MAX_MESSAGE_CHARS
    assert answer.message.startswith("word word ")
    assert answer.message.endswith("\N{HORIZONTAL ELLIPSIS}")


def test_a_failure_without_a_message_has_none(stand_in):
    answer = emitter_version(command=stand_in(stderr="\n \n", exit_code=1))

    assert answer == CommandFailure("version", "exited", exit_code=1, message=None)


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #


def test_status_returns_every_run_in_order(stand_in):
    answer = emitter_status(command=stand_in(stdout=printed({"runs": DOCUMENTED_RUNS})))

    assert answer == EmitterStatus(
        (
            EventQueueRun("finished-run", "modal", "pending", None, 0),
            EventQueueRun(
                "refused-run", "modal", "local_only", "run_registration_rejected", 1
            ),
        )
    )


def test_status_without_runs_is_an_empty_answer(stand_in):
    answer = emitter_status(command=stand_in(stdout=printed({"runs": []})))

    assert answer == EmitterStatus(())


def test_status_ignores_keys_it_does_not_document(stand_in):
    runs = [{**DOCUMENTED_RUNS[0], "later_field": [1, {"deep": True}]}]

    answer = emitter_status(
        command=stand_in(stdout=printed({"runs": runs, "summary": {}}))
    )

    assert answer == EmitterStatus(
        (EventQueueRun("finished-run", "modal", "pending", None, 0),)
    )


def run_with(**changes):
    return [{**DOCUMENTED_RUNS[0], **changes}]


def run_without(name):
    return [{key: value for key, value in DOCUMENTED_RUNS[0].items() if key != name}]


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json\n",
        printed([]),
        printed({}),
        printed({"runs": {}}),
        printed({"runs": ["finished-run"]}),
        printed({"runs": run_without("reason")}),
        printed({"runs": run_without("undelivered_events")}),
        printed({"runs": run_with(run_id=5)}),
        printed({"runs": run_with(producer_id=None)}),
        printed({"runs": run_with(upload_state=["pending"])}),
        printed({"runs": run_with(reason=0)}),
        printed({"runs": run_with(undelivered_events=-1)}),
        printed({"runs": run_with(undelivered_events=True)}),
        printed({"runs": run_with(undelivered_events=1.0)}),
        printed({"runs": run_with(undelivered_events="1")}),
    ],
    ids=[
        "nothing",
        "not JSON",
        "a list",
        "no runs",
        "runs not a list",
        "a run not an object",
        "a run without reason",
        "a run without its count",
        "run ID a number",
        "producer ID null",
        "upload state a list",
        "reason a number",
        "negative count",
        "boolean count",
        "count a float",
        "count as text",
    ],
)
def test_status_output_that_is_not_its_documented_runs_is_unusable(stand_in, stdout):
    answer = emitter_status(command=stand_in(stdout=stdout))

    assert isinstance(answer, CommandFailure)
    assert (answer.command, answer.reason, answer.exit_code) == (
        "status",
        "unusable_output",
        0,
    )
    assert answer.output_problem


@pytest.mark.parametrize(
    ("exit_code", "reason"),
    [
        (8, "no_event_queue_file"),
        (9, "unusable_event_queue_file"),
        (2, "usage_error"),
        (1, "exited"),
        (3, "exited"),
    ],
)
def test_each_status_exit_code_is_its_own_failure(stand_in, exit_code, reason):
    message = "No event queue file exists at /tmp/events.sqlite3; nothing was read."

    answer = emitter_status(
        command=stand_in(stderr=message + "\n", exit_code=exit_code)
    )

    assert answer == CommandFailure(
        "status", reason, exit_code=exit_code, message=message
    )


# --------------------------------------------------------------------------- #
# telemetry-token                                                              #
# --------------------------------------------------------------------------- #


def test_telemetry_token_returns_the_documented_answer(stand_in):
    answer = telemetry_token(
        TOKEN_LIFETIME_SECONDS, command=stand_in(stdout=printed(DOCUMENTED_TOKEN))
    )

    assert answer == TelemetryToken(
        STAND_IN_TOKEN, "2026-11-27T06:00:00+00:00", "https://collector.example"
    )
    assert answer.expiry == datetime(2026, 11, 27, 6, tzinfo=UTC)


def test_telemetry_token_repr_and_text_leave_the_token_out(stand_in):
    answer = telemetry_token(
        TOKEN_LIFETIME_SECONDS, command=stand_in(stdout=printed(DOCUMENTED_TOKEN))
    )

    assert isinstance(answer, TelemetryToken)
    assert answer.access_token == STAND_IN_TOKEN
    assert STAND_IN_TOKEN not in repr(answer)
    assert STAND_IN_TOKEN not in str(answer)
    assert "2026-11-27T06:00:00+00:00" in repr(answer)


def test_telemetry_token_origin_loses_its_trailing_slash(stand_in):
    document = {**DOCUMENTED_TOKEN, "collector_origin": "https://collector.example/"}

    answer = telemetry_token(3600, command=stand_in(stdout=printed(document)))

    assert isinstance(answer, TelemetryToken)
    assert answer.collector_origin == "https://collector.example"


def test_a_loopback_origin_is_accepted_only_with_a_development_collector(stand_in):
    document = {**DOCUMENTED_TOKEN, "collector_origin": "http://127.0.0.1:8123"}
    command = stand_in(stdout=printed(document))

    production = telemetry_token(3600, command=command)
    development = telemetry_token(
        3600, command=command, development_collector_url="http://127.0.0.1:8123"
    )

    assert isinstance(production, CommandFailure)
    assert production.reason == "unusable_output"
    assert development == TelemetryToken(
        STAND_IN_TOKEN, "2026-11-27T06:00:00+00:00", "http://127.0.0.1:8123"
    )


@pytest.mark.parametrize(
    ("exit_code", "reason"),
    [
        (2, "usage_error"),
        (3, "missing_credential"),
        (4, "rejected_credential"),
        (5, "collector_unavailable"),
        (6, "lifetime_unsupported"),
        (7, "unexpected_failure"),
        (1, "exited"),
        (8, "exited"),
    ],
)
def test_each_telemetry_token_exit_code_is_its_own_failure(stand_in, exit_code, reason):
    message = "The collector refused the Hugging Face credential (HTTP 403)."

    answer = telemetry_token(
        3600, command=stand_in(stderr=message + "\n", exit_code=exit_code)
    )

    assert answer == CommandFailure(
        "telemetry-token", reason, exit_code=exit_code, message=message
    )


def test_a_failed_telemetry_token_never_passes_on_its_standard_output(stand_in):
    # A misbehaving command that printed the token and quoted it on standard
    # error: the failure withholds the message, since standard output was
    # not empty.
    answer = telemetry_token(
        3600,
        command=stand_in(
            stdout=printed(DOCUMENTED_TOKEN),
            stderr=f"refused {STAND_IN_TOKEN}\n",
            exit_code=4,
        ),
    )

    assert answer == CommandFailure(
        "telemetry-token", "rejected_credential", exit_code=4, message=None
    )
    assert STAND_IN_TOKEN not in repr(answer)


def token_with(**changes):
    return printed({**DOCUMENTED_TOKEN, **changes})


def token_without(name):
    return printed(
        {key: value for key, value in DOCUMENTED_TOKEN.items() if key != name}
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        f"{STAND_IN_TOKEN}\n",
        printed([STAND_IN_TOKEN]),
        token_without("access_token"),
        token_with(access_token=""),
        token_with(access_token=f"{STAND_IN_TOKEN} second"),
        token_with(access_token=f"{STAND_IN_TOKEN}\nsecond"),
        token_with(access_token=f"{STAND_IN_TOKEN}é"),
        token_with(access_token=5),
        token_without("expires_at"),
        token_with(expires_at="2026-11-27T06:00:00"),
        token_with(expires_at="tomorrow"),
        token_with(expires_at=1_800_000_000),
        token_without("collector_origin"),
        token_with(collector_origin="http://collector.example"),
        token_with(collector_origin="https://collector.example/v1"),
        token_with(collector_origin="https://collector.example?region=1"),
        token_with(collector_origin="https://user@collector.example"),
        token_with(collector_origin="https://collector.example\n"),
        token_with(collector_origin="https://collector.example:port"),
        token_with(collector_origin="https://"),
        token_with(collector_origin=None),
    ],
    ids=[
        "nothing",
        "the token alone",
        "a list",
        "no token",
        "empty token",
        "token with a space",
        "token with a line break",
        "token with a non-ASCII character",
        "token a number",
        "no expiry",
        "expiry without a time zone",
        "expiry not a time",
        "expiry a number",
        "no origin",
        "origin over HTTP",
        "origin with a path",
        "origin with a query",
        "origin with a user",
        "origin with a line break",
        "origin with an unusable port",
        "origin without a host",
        "origin null",
    ],
)
def test_telemetry_token_output_that_is_not_a_usable_token_is_unusable(
    stand_in, stdout
):
    answer = telemetry_token(3600, command=stand_in(stdout=stdout))

    assert isinstance(answer, CommandFailure)
    assert (answer.command, answer.reason, answer.exit_code) == (
        "telemetry-token",
        "unusable_output",
        0,
    )
    assert answer.output_problem
    assert STAND_IN_TOKEN not in repr(answer)


def test_output_that_is_not_utf_8_is_read_with_replacement_characters(
    emitter_command_script,
):
    command = emitter_command_script(
        "not-utf-8",
        "import sys\n"
        'sys.stdout.buffer.write(b\'{"emitter_version": "0.17.0\\\\xff"}\')\n'
        "sys.stderr.buffer.write(b'\\xfe\\n')\n"
        "sys.exit(1 if len(sys.argv) > 1 and sys.argv[1] == 'status' else 0)\n",
    )

    version_answer = emitter_version(command=command)
    status_answer = emitter_status(command=command)

    assert version_answer == EmitterVersion(
        "0.17.0\N{REPLACEMENT CHARACTER}", None, None
    )
    assert status_answer == CommandFailure(
        "status", "exited", exit_code=1, message="\N{REPLACEMENT CHARACTER}"
    )


def test_a_command_killed_by_a_signal_is_exited_with_its_negative_code(
    emitter_command_script,
):
    killed = emitter_command_script(
        "killed", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    )

    answers = [
        run_call(call, command=killed)
        for call in ("version", "status", "telemetry-token")
    ]

    assert answers == [
        CommandFailure(call, "exited", exit_code=-9)
        for call in ("version", "status", "telemetry-token")
    ]

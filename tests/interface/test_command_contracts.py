"""The interface's command calls against this emitter's real commands.

`fixtures/commands/` records what `version`, `status` and `telemetry-token`
print, as the README documents them. These tests check both sides: each
call parses its recorded output, and the installed `microcosm-emitter`
prints exactly the recorded keys and answers each call, so neither the
commands nor the calls can change on their own.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

import pytest
from fake_collector import FakeCollector

from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS
from microcosm_emitter.core.collector_origin import resolve_collector_origin
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.spool import EventSpool
from microcosm_emitter_interface.commands import (
    COMMAND_NAMES,
    CommandFailure,
    EmitterStatus,
    EmitterVersion,
    EventQueueRun,
    TelemetryToken,
    emitter_status,
    emitter_version,
    recorded_command_output,
    telemetry_token,
)
from microcosm_emitter_interface.launcher import DEFAULT_COMMAND

INSTALLED_COMMAND = shutil.which(DEFAULT_COMMAND)
#: Long enough for a cold start of the installed command on a CI runner.
COMMAND_TIMEOUT_SECONDS = 60
SYNTHETIC_HUGGING_FACE_TOKEN = "hf_synthetic_operator_token_for_contracts"
TOKEN_LIFETIME_SECONDS = 18 * 3600
UNREACHABLE_COLLECTOR_URL = "http://127.0.0.1:1"


@pytest.fixture
def installed_command():
    assert INSTALLED_COMMAND is not None, "the emitter's command must be installed"
    return INSTALLED_COMMAND


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


def recorded(command):
    return json.loads(recorded_command_output(command))


def printed_by(installed_command, *arguments, environment=None):
    """Run the installed command; return what it printed as JSON."""
    completed = subprocess.run(
        [installed_command, *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def write_event_queue_file(path):
    """Queue two runs, one with two undelivered events and one with none."""
    queue = EventSpool(path)
    try:
        for run_id, event_count in (("waiting-run", 2), ("finished-run", 0)):
            run = TelemetryRun(
                run_id=run_id, country_code="US", pipeline="us_acs", producer_id="modal"
            ).as_registration()
            queue.register(run)
            for index in range(event_count):
                queue.append(
                    run,
                    {
                        "event_type": "stage",
                        "stage_id": f"s{index}",
                        "status": "started",
                    },
                )
        queue.record_pending_reason("waiting-run", "modal", "collector_token_expired")
    finally:
        queue.close()


EXPECTED_STATUS = EmitterStatus(
    (
        EventQueueRun("finished-run", "modal", "pending", None, 0),
        EventQueueRun("waiting-run", "modal", "pending", "collector_token_expired", 2),
    )
)


def token_environment():
    """Return this process's environment with a synthetic Hugging Face login."""
    return {**os.environ, "HF_TOKEN": SYNTHETIC_HUGGING_FACE_TOKEN}


# --------------------------------------------------------------------------- #
# The recorded outputs                                                         #
# --------------------------------------------------------------------------- #


def test_every_command_has_a_recorded_output():
    assert COMMAND_NAMES == ("version", "status", "telemetry-token")
    for command in COMMAND_NAMES:
        output = recorded_command_output(command)
        assert output.endswith("\n")
        assert output.count("\n") == 1
        assert isinstance(json.loads(output), dict)


def test_an_unknown_command_has_no_recorded_output():
    with pytest.raises(ValueError, match="Unknown emitter command"):
        recorded_command_output("host")


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_each_call_parses_its_recorded_output(emitter_command_script, command):
    stand_in = emitter_command_script(
        "recorded-emitter",
        f"import sys\nsys.stdout.write({recorded_command_output(command)!r})\n",
    )
    document = recorded(command)

    if command == "version":
        answer = emitter_version(command=stand_in)
        expected = EmitterVersion(
            document["emitter_version"],
            tuple(document["protocol_versions"]),
            document["collector_origin"],
        )
    elif command == "status":
        answer = emitter_status(command=stand_in)
        expected = EmitterStatus(
            tuple(EventQueueRun(**run) for run in document["runs"])
        )
    else:
        answer = telemetry_token(TOKEN_LIFETIME_SECONDS, command=stand_in)
        expected = TelemetryToken(**document)

    assert answer == expected


# --------------------------------------------------------------------------- #
# version                                                                      #
# --------------------------------------------------------------------------- #


def test_installed_version_prints_the_recorded_keys(installed_command):
    assert sorted(printed_by(installed_command, "version")) == sorted(
        recorded("version")
    )


def test_installed_version_answers_the_call(installed_command):
    answer = emitter_version(command=installed_command, timeout=COMMAND_TIMEOUT_SECONDS)

    assert answer == EmitterVersion(
        version("microcosm-emitter"),
        tuple(SUPPORTED_PROTOCOL_VERSIONS),
        resolve_collector_origin(),
    )


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #


def test_installed_status_prints_the_recorded_keys(installed_command, tmp_path):
    queue_file = tmp_path / "events.sqlite3"
    write_event_queue_file(queue_file)

    printed = printed_by(
        installed_command, "status", f"--event-queue-file={queue_file}"
    )

    expected = recorded("status")
    assert sorted(printed) == sorted(expected)
    assert printed["runs"]
    for run in printed["runs"]:
        assert sorted(run) == sorted(expected["runs"][0])


def test_installed_status_answers_the_call(installed_command, tmp_path):
    queue_file = tmp_path / "events.sqlite3"
    write_event_queue_file(queue_file)

    answer = emitter_status(
        command=installed_command,
        event_queue_file=queue_file,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert answer == EXPECTED_STATUS


def test_installed_status_reads_the_default_file_of_the_given_environment(
    installed_command, tmp_path
):
    cache = tmp_path / "cache"
    write_event_queue_file(cache / "microcosm" / "telemetry" / "events.sqlite3")

    answer = emitter_status(
        command=installed_command,
        environment={**os.environ, "XDG_CACHE_HOME": str(cache)},
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert answer == EXPECTED_STATUS


def test_installed_status_without_a_file_is_no_event_queue_file(
    installed_command, tmp_path
):
    missing = tmp_path / "missing" / "events.sqlite3"

    answer = emitter_status(
        command=installed_command,
        event_queue_file=missing,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert isinstance(answer, CommandFailure)
    assert (answer.reason, answer.exit_code) == ("no_event_queue_file", 8)
    assert answer.message is not None
    assert str(missing) in answer.message
    assert not missing.parent.exists()


def test_installed_status_of_another_file_is_unusable_event_queue_file(
    installed_command, tmp_path
):
    other = tmp_path / "events.sqlite3"
    other.write_text("not an event queue file")

    answer = emitter_status(
        command=installed_command,
        event_queue_file=other,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert isinstance(answer, CommandFailure)
    assert (answer.reason, answer.exit_code) == ("unusable_event_queue_file", 9)
    assert answer.message is not None
    assert str(other) in answer.message


# --------------------------------------------------------------------------- #
# telemetry-token                                                              #
# --------------------------------------------------------------------------- #


def test_installed_telemetry_token_prints_the_recorded_keys(
    installed_command, collector
):
    printed = printed_by(
        installed_command,
        "telemetry-token",
        "--lifetime-seconds=3600",
        f"--development-collector-url={collector.origin}",
        environment=token_environment(),
    )

    assert sorted(printed) == sorted(recorded("telemetry-token"))


def test_installed_telemetry_token_answers_the_call(installed_command, collector):
    requested_at = datetime.now(UTC)

    answer = telemetry_token(
        TOKEN_LIFETIME_SECONDS,
        command=installed_command,
        development_collector_url=collector.origin,
        environment=token_environment(),
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert isinstance(answer, TelemetryToken), answer
    assert answer.collector_origin == collector.origin
    assert answer.access_token.startswith("session-token-")
    assert answer.access_token not in repr(answer)
    lifetime = answer.expiry - requested_at
    assert timedelta(seconds=TOKEN_LIFETIME_SECONDS - 2) <= lifetime
    assert lifetime <= timedelta(seconds=TOKEN_LIFETIME_SECONDS + 1)
    assert collector.exchange_requests == [
        {"scope": "telemetry:write", "lifetime_seconds": TOKEN_LIFETIME_SECONDS}
    ]
    assert collector.exchange_credentials == [SYNTHETIC_HUGGING_FACE_TOKEN]


@pytest.mark.parametrize(
    ("failure", "reason", "exit_code"),
    [
        ("no Hugging Face login", "missing_credential", 3),
        ("credential refused with 401", "rejected_credential", 4),
        ("collector unreachable", "collector_unavailable", 5),
        ("collector without lifetime_seconds (422)", "lifetime_unsupported", 6),
        ("development collector that is not loopback", "usage_error", 2),
    ],
)
def test_installed_telemetry_token_failures_are_typed(
    installed_command, collector, failure, reason, exit_code
):
    environment = token_environment()
    collector_url = collector.origin
    if failure == "no Hugging Face login":
        del environment["HF_TOKEN"]
    elif failure == "credential refused with 401":
        collector.exchange_status = 401
    elif failure == "collector unreachable":
        collector_url = UNREACHABLE_COLLECTOR_URL
    elif failure == "collector without lifetime_seconds (422)":
        collector.accepts_lifetime_seconds = False
    else:
        collector_url = "https://collector.example"

    answer = telemetry_token(
        3600,
        command=installed_command,
        development_collector_url=collector_url,
        environment=environment,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    assert isinstance(answer, CommandFailure), answer
    assert (answer.command, answer.reason, answer.exit_code) == (
        "telemetry-token",
        reason,
        exit_code,
    )
    assert answer.message
    assert SYNTHETIC_HUGGING_FACE_TOKEN not in repr(answer)

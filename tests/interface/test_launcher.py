"""The interface starts the installed emitter command as the build's own child."""

import os
import shutil
import time
from pathlib import Path

import psutil
import pytest

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import (
    COMMAND_ENVIRONMENT_VARIABLE,
    DEFAULT_COMMAND,
    EmitterStartupError,
    resolve_command,
    start_emitter,
)
from microcosm_emitter_interface.orrery import OrrerySettings
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings

INSTALLED_COMMAND = shutil.which(DEFAULT_COMMAND)
FAKE_HF_TOKEN = "hf_fake_value_from_hf_token"
FAKE_HUGGINGFACE_TOKEN = "hf_fake_value_from_huggingface_token"
FAKE_SAVED_LOGIN_TOKEN = "hf_fake_value_from_saved_login"


def idle_orrery() -> OrrerySettings:
    return OrrerySettings(
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
    )


def test_explicit_command_wins_over_the_environment_and_the_path(
    isolated_path, emitter_command_script, monkeypatch
):
    explicit = emitter_command_script("explicit-emitter", "")
    configured = emitter_command_script("configured-emitter", "")
    on_path = isolated_path / DEFAULT_COMMAND
    shutil.copy(explicit, on_path)
    monkeypatch.setenv(COMMAND_ENVIRONMENT_VARIABLE, str(configured))
    assert resolve_command(explicit) == str(explicit)


def test_environment_variable_names_the_command_when_no_argument_does(
    isolated_path, emitter_command_script, monkeypatch
):
    configured = emitter_command_script("configured-emitter", "")
    shutil.copy(configured, isolated_path / DEFAULT_COMMAND)
    monkeypatch.setenv(COMMAND_ENVIRONMENT_VARIABLE, str(configured))
    assert resolve_command() == str(configured)


def test_installed_command_on_the_path_is_the_default(
    isolated_path, emitter_command_script, monkeypatch
):
    monkeypatch.delenv(COMMAND_ENVIRONMENT_VARIABLE, raising=False)
    on_path = isolated_path / DEFAULT_COMMAND
    shutil.copy(emitter_command_script("installed", ""), on_path)
    assert resolve_command() == str(on_path)


def test_missing_command_is_reported_without_starting_anything(
    isolated_path, monkeypatch
):
    monkeypatch.delenv(COMMAND_ENVIRONMENT_VARIABLE, raising=False)
    with pytest.raises(EmitterStartupError) as failure:
        resolve_command()
    assert failure.value.reason == "command_not_found"


def test_client_without_an_installed_emitter_is_unavailable_and_never_raises(
    tmp_path, isolated_path, monkeypatch, capsys
):
    monkeypatch.delenv(COMMAND_ENVIRONMENT_VARIABLE, raising=False)
    client = EmitterClient.start(
        telemetry=TelemetrySettings(run=TelemetryRun("no-emitter", "UK", "test")),
        spool_path=tmp_path / "events.sqlite3",
    )
    assert not client.available
    assert client.startup_error is not None
    assert client.startup_error.reason == "command_not_found"
    client.telemetry.stage("compile")
    client.telemetry.complete()
    client.close()
    assert "the build will continue" in capsys.readouterr().err


def test_emitter_runs_as_a_child_of_the_build_under_the_same_user(tmp_path):
    assert INSTALLED_COMMAND is not None
    process = start_emitter(
        {"orrery": idle_orrery().configuration(tmp_path / "events.sqlite3")},
        command=INSTALLED_COMMAND,
    )
    try:
        child = psutil.Process(process.process.pid)
        assert child.ppid() == os.getpid()
        assert child.uids().real == os.getuid()
        assert "spool_path" not in " ".join(child.cmdline())
        assert process.description["modules"] == ["orrery"]
        assert PROTOCOL_VERSION in process.description["protocol_versions"]
    finally:
        process.close()
    assert process.process.wait(timeout=20) == 0


@pytest.mark.parametrize(
    ("variables", "saved_login", "expected_token"),
    [
        (
            {"HF_TOKEN": FAKE_HF_TOKEN, "HUGGINGFACE_TOKEN": FAKE_HUGGINGFACE_TOKEN},
            FAKE_SAVED_LOGIN_TOKEN,
            FAKE_HF_TOKEN,
        ),
        (
            {"HUGGINGFACE_TOKEN": FAKE_HUGGINGFACE_TOKEN},
            FAKE_SAVED_LOGIN_TOKEN,
            FAKE_HUGGINGFACE_TOKEN,
        ),
        ({}, FAKE_SAVED_LOGIN_TOKEN, FAKE_SAVED_LOGIN_TOKEN),
    ],
    ids=["hf-token-first", "huggingface-token-second", "saved-login-last"],
)
def test_child_finds_the_build_hugging_face_credential_in_the_same_order(
    tmp_path, monkeypatch, loopback_collector, variables, saved_login, expected_token
):
    """The token reaches the child only by inheriting the build's environment.

    The client passes no credential and no environment: HF_TOKEN, then
    HUGGINGFACE_TOKEN, then the token saved by `hf auth login`
    (core/auth.py), each set only in this build process.
    """
    address, received_requests = loopback_collector
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    token_path = Path(os.environ["HF_TOKEN_PATH"])
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with token_path.open("w") as token_file:
        token_file.write(saved_login)
    client = EmitterClient.start(
        telemetry=TelemetrySettings(
            run=TelemetryRun("credential-order", "UK", "test"),
            development_collector_url=address,
        ),
        spool_path=tmp_path / "events.sqlite3",
        command=INSTALLED_COMMAND,
    )
    assert client.available, client.startup_error
    try:
        client.telemetry.stage("compile")
        client.telemetry.complete()
        deadline = time.monotonic() + 15
        while not received_requests and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        client.close()
    exchange_path, authorization, _ = received_requests[0]
    assert exchange_path == "/v1/auth/huggingface/exchange"
    assert authorization == f"Bearer {expected_token}"


def test_emitter_that_does_not_accept_this_protocol_version_is_stopped(
    tmp_path, emitter_command_script
):
    future_only = emitter_command_script(
        "future-emitter",
        "import sys\n"
        "from microcosm_emitter.host.__main__ import main\n"
        "description = {'emitter_version': '99.0.0', 'protocol_versions': [99]}\n"
        "sys.exit(main(sys.argv[2:], description=description))\n",
    )
    with pytest.raises(EmitterStartupError) as failure:
        start_emitter(
            {"orrery": idle_orrery().configuration(tmp_path / "events.sqlite3")},
            command=future_only,
        )
    assert failure.value.reason == "unsupported_protocol_version"


def test_emitter_that_exits_reports_only_its_exit_code(
    tmp_path, emitter_command_script
):
    exits = emitter_command_script(
        "exiting-emitter",
        "import sys\nsys.stdin.read()\nprint('private detail', file=sys.stderr)\n"
        "sys.exit(3)\n",
    )
    with pytest.raises(EmitterStartupError) as failure:
        start_emitter(
            {"orrery": {"spool_path": "private-configuration-value"}},
            command=exits,
            startup_timeout=10,
        )
    assert failure.value.reason == "child_exit"
    assert failure.value.exit_code == 3
    assert "private" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_close_stops_the_emitter_and_removes_its_runtime_directory(tmp_path):
    process = start_emitter(
        {"orrery": idle_orrery().configuration(tmp_path / "events.sqlite3")},
        command=INSTALLED_COMMAND,
    )
    process.close()
    assert process.process.wait(timeout=20) == 0
    deadline = time.monotonic() + 5
    while process.runtime_directory.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process.runtime_directory.exists()


class AnsweringTransport:
    """A socket transport stand-in that answers ``hello`` with a fixed value."""

    def __init__(self, description):
        self.description = description

    def hello(self, *, timeout=None):
        return self.description


@pytest.mark.parametrize(
    "description",
    [
        {"protocol_versions": [PROTOCOL_VERSION], "modules": [{"name": "orrery"}]},
        {"protocol_versions": [PROTOCOL_VERSION], "modules": ["orrery", 3]},
        {"protocol_versions": [PROTOCOL_VERSION, {"version": 2}], "modules": []},
    ],
    ids=["module-objects", "module-number", "version-object"],
)
def test_a_hello_answer_with_unusable_values_is_refused(description):
    """A later emitter's differently shaped answer stops at startup, never in a call.

    Every call reads the module names as a set of strings.
    """
    from microcosm_emitter_interface.launcher import _accepted_description

    with pytest.raises(EmitterStartupError) as failure:
        _accepted_description(AnsweringTransport(description), time.monotonic() + 5)
    assert failure.value.reason == "unsupported_protocol_version"

"""Each message both packages write reaches its stream exactly as print wrote it.

For every place that writes a message, these tests pin the calls on the
stream: a write of the text, a write of the newline, then a flush where that
place flushes. The same calls give the same bytes, the same encoding and the
same errors from the real stream. As print does, a message for standard error
goes to standard output while there is no standard error, nothing is written
when there is no stream either, and an error the stream raises reaches the
caller.
"""

from __future__ import annotations

import errno
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from recording_streams import RecordingStream, printed_calls

from microcosm_emitter.core.auth import (
    GivenCollectorTokenError,
    UnreadableLoginError,
    shared_session,
)
from microcosm_emitter.core.constants import (
    COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
    COLLECTOR_TOKEN_ORIGIN_VARIABLE,
    COLLECTOR_TOKEN_VARIABLE,
)
from microcosm_emitter.huggingface.service.delivery import HuggingFaceDelivery
from microcosm_emitter.orrery import cli
from microcosm_emitter.orrery.service.module import OrreryModule
from microcosm_emitter.submissions.delivery import print_to_standard_error
from microcosm_emitter.telemetry import client as telemetry_client
from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter_interface import client as interface_client
from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess, EmitterStartupError

if TYPE_CHECKING:
    from collections.abc import Callable

SAMPLE_MESSAGE = "A sample message for the build's log."
PUBLISHED_RECEIPT = {
    "publication_id": "pub-1",
    "status": "published",
    "message": "café",
}
FAILED_RECEIPT = {
    "publication_id": "pub-1",
    "status": "failed",
    "error_code": "publication_invalid_evidence_or_response",
}


@dataclass(frozen=True)
class Message:
    """One place that writes a message: how to reach it, and what it writes."""

    write: Callable[[pytest.MonkeyPatch, object], None]
    stream: str
    text: str
    flush: bool
    raises: type[Exception] | None = None


def given_token_start_error(monkeypatch, tmp_path):
    monkeypatch.setenv(COLLECTOR_TOKEN_VARIABLE, "synthetic-collector-token")
    monkeypatch.delenv(COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE, raising=False)
    monkeypatch.delenv(COLLECTOR_TOKEN_ORIGIN_VARIABLE, raising=False)
    shared_session({}, "https://collector.example")


def hugging_face_warning(monkeypatch, tmp_path):
    delivery = HuggingFaceDelivery(None, api_factory=lambda: None)
    delivery.warn_once("upload", SAMPLE_MESSAGE)
    delivery.warn_once("upload", SAMPLE_MESSAGE)


class FakeGraphClient:
    @classmethod
    def start(cls, **_settings):
        return cls()

    def enqueue(self, directory, inventory):
        return dict(FAILED_RECEIPT)

    def retry(self, publication_id, *, wait_seconds):
        return dict(PUBLISHED_RECEIPT)

    def close(self):
        pass


def failed_enqueue_receipt(monkeypatch, tmp_path):
    directory = tmp_path / "graph"
    directory.mkdir()
    inventory = json.dumps({"publication_id": "pub-1"})
    (directory / "orrery.upload.json").write_text(inventory)
    monkeypatch.setattr(cli, "GraphPublicationClient", FakeGraphClient)
    arguments = ["--publication-id", "pub-1", "--directory", str(directory)]
    assert cli.main(arguments) == 1


def retry_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "GraphPublicationClient", FakeGraphClient)
    assert cli.main(["--publication-id", "pub-1"]) == 0


def graph_worker_error(monkeypatch, tmp_path):
    class FailingDelivery:
        def flush_once(self):
            error_message = "synthetic queue failure"
            raise OSError(error_message)

    module = OrreryModule({}, None)
    module.delivery = FailingDelivery()
    module.tick(0.0)
    module.tick(1.0)


def submission_outcome(monkeypatch, tmp_path):
    print_to_standard_error(SAMPLE_MESSAGE)


def telemetry_start_warning(monkeypatch, tmp_path):
    def fail(*arguments, **settings):
        error_message = "synthetic startup failure"
        raise PermissionError(error_message)

    monkeypatch.setattr(telemetry_client, "start_service", fail)
    LocalTelemetryEmitter.start(
        run=TelemetryRun("run", "UK", "test"), spool_path=tmp_path / "queue"
    )


def telemetry_queue_warning(monkeypatch, tmp_path):
    class FailingTransport:
        def send(self, request):
            error_message = "synthetic send failure"
            raise OSError(error_message)

        def close(self):
            pass

    emitter = LocalTelemetryEmitter(
        run=TelemetryRun("run", "UK", "test"), transport=FailingTransport()
    )
    emitter.stage("one")
    emitter.stage("two")


class FakeSpool:
    def make_local_only(self, run_id, producer_id, reason):
        pass

    def record_pending_reason(self, run_id, producer_id, reason):
        pass


class FakeSession:
    def __init__(self, answer):
        self.answer = answer

    def credential(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def invalidate(self):
        pass


def collector_answer(answer):
    """Return a writer that asks the collector delivery for a token twice."""

    def ask_twice(monkeypatch, tmp_path):
        delivery = CollectorDelivery(
            FakeSpool(),
            development_collector_url="http://127.0.0.1:9",
            session=FakeSession(answer),
        )
        registration = {"run_id": "run", "producer_id": "producer"}
        delivery._collector_token(registration)
        delivery._collector_token(registration)

    return ask_twice


def interface_invalid_configuration(monkeypatch, tmp_path):
    # Settings without a configuration method: an invalid configuration.
    EmitterClient.start(telemetry=object(), spool_path=tmp_path / "queue")


def interface_startup_failure(error):
    """Return a writer whose emitter fails to start with ``error``."""

    def start(monkeypatch, tmp_path):
        def fail(*arguments, **settings):
            raise error

        monkeypatch.setattr(interface_client, "start_emitter", fail)
        EmitterClient.start(spool_path=tmp_path / "queue")

    return start


class AcceptingTransport:
    def send(self, module, message, *, timeout=None):
        pass

    def query(self, module, message, *, timeout=None):
        return {}

    def close(self):
        pass


class MultiElementArray:
    """Behaves like a NumPy array with more than one element."""

    def item(self):
        error_message = "can only convert an array of size 1 to a Python scalar"
        raise ValueError(error_message)


def interface_refused_request(monkeypatch, tmp_path):
    process = EmitterProcess(
        process=None,
        transport=AcceptingTransport(),
        runtime_directory=Path("/nonexistent"),
        description={"modules": ["telemetry"], "protocol_versions": [1]},
    )
    client = EmitterClient(process)
    client.telemetry.stage("compile", value=MultiElementArray())
    client.telemetry.stage("compile", value=MultiElementArray())


MESSAGES = {
    "core.auth: an unusable given token": Message(
        given_token_start_error,
        "stderr",
        "The Microcosm emitter cannot start: MICROCOSM_COLLECTOR_TOKEN, "
        "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT and MICROCOSM_COLLECTOR_ORIGIN must "
        "be set together. It exits before accepting any request from the build.",
        flush=True,
        raises=GivenCollectorTokenError,
    ),
    "huggingface delivery: a warning": Message(
        hugging_face_warning, "stderr", SAMPLE_MESSAGE, flush=True
    ),
    "publish-graph: a failed enqueue": Message(
        failed_enqueue_receipt,
        "stdout",
        '{"error_code": "publication_invalid_evidence_or_response", '
        '"publication_id": "pub-1", "status": "failed"}',
        flush=False,
    ),
    "publish-graph: a retry": Message(
        retry_receipt,
        "stdout",
        '{"message": "caf\\u00e9", "publication_id": "pub-1", "status": "published"}',
        flush=False,
    ),
    "orrery module: a queue error": Message(
        graph_worker_error,
        "stderr",
        "Microcosm graph publication hit a local queue error; pending jobs remain "
        "queued for a later attempt.",
        flush=True,
    ),
    "submissions: an outcome": Message(
        submission_outcome, "stderr", SAMPLE_MESSAGE, flush=True
    ),
    "telemetry client: a startup failure": Message(
        telemetry_start_warning,
        "stderr",
        "Local telemetry emitter service failed to start (Service startup failed "
        "(startup_failure).); the build will continue.",
        flush=False,
    ),
    "telemetry client: a queue failure": Message(
        telemetry_queue_warning,
        "stderr",
        "Telemetry could not be queued (OSError); the build will continue.",
        flush=False,
    ),
    "collector: no credential": Message(
        collector_answer(("missing", None)),
        "stderr",
        "Microcosm telemetry is local-only: no ambient Hugging Face credential was "
        "found. The dataset build will continue.",
        flush=True,
    ),
    "collector: an unreadable login": Message(
        collector_answer(UnreadableLoginError("PermissionError")),
        "stderr",
        "Microcosm telemetry is waiting: the Hugging Face login saved by `hf auth "
        "login` could not be read (PermissionError), and neither HF_TOKEN nor "
        "HUGGINGFACE_TOKEN is set. Queued events stay pending, and are delivered "
        "once the login can be read. The dataset build will continue.",
        flush=True,
    ),
    "collector: an unusable given token": Message(
        collector_answer(("token_expired", None)),
        "stderr",
        "Microcosm telemetry is waiting: the collector token this emitter was "
        "given has expired or was refused, and it cannot be renewed here. Queued "
        "events stay pending for delivery by an emitter with a Hugging Face login. "
        "The dataset build will continue.",
        flush=True,
    ),
    "collector: a refused credential": Message(
        collector_answer(("rejected", None)),
        "stderr",
        "Microcosm telemetry is local-only for this run: the ambient Hugging Face "
        "credential was not accepted as a PolicyEngine organization member. The "
        "dataset build will continue.",
        flush=True,
    ),
    "interface client: an invalid configuration": Message(
        interface_invalid_configuration,
        "stderr",
        "Local Microcosm emitter failed to start (invalid_configuration); the "
        "build will continue.",
        flush=False,
    ),
    "interface client: a startup error": Message(
        interface_startup_failure(EmitterStartupError("readiness_timeout")),
        "stderr",
        "Local Microcosm emitter failed to start (readiness_timeout); the build "
        "will continue.",
        flush=False,
    ),
    "interface client: an unexpected startup failure": Message(
        interface_startup_failure(RuntimeError("synthetic startup failure")),
        "stderr",
        "Local Microcosm emitter failed to start (startup_failure); the build will "
        "continue.",
        flush=False,
    ),
    "interface client: a refused request": Message(
        interface_refused_request,
        "stderr",
        "The local Microcosm emitter did not accept a telemetry request "
        "(invalid_arguments, ValueError); the build will continue.",
        flush=False,
    ),
}
EACH_MESSAGE = pytest.mark.parametrize("message", MESSAGES.values(), ids=list(MESSAGES))
EACH_STANDARD_ERROR_MESSAGE = pytest.mark.parametrize(
    "message",
    [message for message in MESSAGES.values() if message.stream == "stderr"],
    ids=[name for name, message in MESSAGES.items() if message.stream == "stderr"],
)
EACH_FLUSHED_MESSAGE = pytest.mark.parametrize(
    "message",
    [message for message in MESSAGES.values() if message.flush],
    ids=[name for name, message in MESSAGES.items() if message.flush],
)
STREAM_ERRORS = pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError(errno.EPIPE, "Broken pipe"),
        ValueError("I/O operation on closed file."),
    ],
    ids=["broken pipe", "closed stream"],
)


def write_message(message, monkeypatch, tmp_path):
    if message.raises is None:
        message.write(monkeypatch, tmp_path)
    else:
        with pytest.raises(message.raises):
            message.write(monkeypatch, tmp_path)


@EACH_MESSAGE
def test_each_message_is_written_as_print_wrote_it(message, monkeypatch, tmp_path):
    streams = {"stdout": RecordingStream(), "stderr": RecordingStream()}
    monkeypatch.setattr("sys.stdout", streams["stdout"])
    monkeypatch.setattr("sys.stderr", streams["stderr"])
    write_message(message, monkeypatch, tmp_path)
    assert streams[message.stream].calls == printed_calls(
        message.text, flush=message.flush
    )
    other = "stdout" if message.stream == "stderr" else "stderr"
    assert streams[other].calls == []


@EACH_STANDARD_ERROR_MESSAGE
def test_without_standard_error_a_message_goes_to_standard_output(
    message, monkeypatch, tmp_path
):
    standard_output = RecordingStream()
    monkeypatch.setattr("sys.stdout", standard_output)
    monkeypatch.setattr("sys.stderr", None)
    write_message(message, monkeypatch, tmp_path)
    assert standard_output.calls == printed_calls(message.text, flush=message.flush)


@EACH_MESSAGE
def test_without_either_stream_nothing_is_written(message, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    write_message(message, monkeypatch, tmp_path)


@EACH_MESSAGE
@STREAM_ERRORS
def test_an_error_writing_the_message_reaches_the_caller(
    message, error, monkeypatch, tmp_path
):
    failing = RecordingStream(fail_on="write", error=error)
    monkeypatch.setattr(f"sys.{message.stream}", failing)
    with pytest.raises(type(error)):
        message.write(monkeypatch, tmp_path)
    assert failing.calls == [("write", message.text)]


@EACH_FLUSHED_MESSAGE
def test_an_error_flushing_the_message_reaches_the_caller(
    message, monkeypatch, tmp_path
):
    failing = RecordingStream(
        fail_on="flush", error=BrokenPipeError(errno.EPIPE, "Broken pipe")
    )
    monkeypatch.setattr(f"sys.{message.stream}", failing)
    with pytest.raises(BrokenPipeError):
        message.write(monkeypatch, tmp_path)
    assert failing.calls == printed_calls(message.text, flush=True)

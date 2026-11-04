"""The bytes each program writes, with the real standard streams of a process.

``microcosm-emitter-publish-graph`` prints its receipt as JSON on standard
output, and both packages write their warnings to standard error. These tests
run each in a child process and pin the bytes it writes, with each stream's
encoding, with a stream closed, and with a pipe whose reader has gone, where
the result is compared with what ``print`` gives in the same process state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PUBLISH_GRAPH = r"""
import sys
from microcosm_emitter.orrery import cli

class FakeGraphClient:
    @classmethod
    def start(cls, **settings):
        return cls()

    def enqueue(self, directory, inventory):
        return {
            "publication_id": "pub-1",
            "status": "failed",
            "error_code": "publication_invalid_evidence_or_response",
        }

    def retry(self, publication_id, *, wait_seconds):
        return {
            "publication_id": publication_id,
            "status": "published",
            "message": "café",
        }

    def close(self):
        pass

cli.GraphPublicationClient = FakeGraphClient
sys.exit(cli.main(sys.argv[1:]))
"""
PRINT_AND_EXIT = "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))"
EMITTER_WARNING = r"""
from microcosm_emitter.submissions.delivery import print_to_standard_error
print_to_standard_error("Résumé of the submissions.")
"""
INTERFACE_WARNING = r"""
from pathlib import Path
from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess

class AcceptingTransport:
    def send(self, module, message, *, timeout=None):
        pass

    def query(self, module, message, *, timeout=None):
        return {}

    def close(self):
        pass

class Unconvertible:
    def item(self):
        raise type("Erreuré", (ValueError,), {})("synthetic")

process = EmitterProcess(
    process=None,
    transport=AcceptingTransport(),
    runtime_directory=Path("/nonexistent"),
    description={"modules": ["telemetry"], "protocol_versions": [1]},
)
EmitterClient(process).telemetry.stage("compile", value=Unconvertible())
"""

RETRY_RECEIPT = (
    '{"message": "caf\\u00e9", "publication_id": "pub-1", "status": "published"}'
)
FAILED_RECEIPT = (
    '{"error_code": "publication_invalid_evidence_or_response", '
    '"publication_id": "pub-1", "status": "failed"}'
)
EMITTER_WARNING_TEXT = "Résumé of the submissions."
INTERFACE_WARNING_TEXT = (
    "The local Microcosm emitter did not accept a telemetry request "
    "(invalid_arguments, Erreuré); the build will continue."
)


def run_child(script, arguments=(), *, encoding="utf-8", redirections=""):
    """Run ``script`` in a child Python whose streams ``redirections`` change."""
    environment = {**os.environ, "PYTHONIOENCODING": encoding}
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'exec "$@" {redirections}',
            "sh",
            sys.executable,
            "-c",
            script,
            *arguments,
        ],
        capture_output=True,
        env=environment,
        check=False,
    )


def run_child_into_a_broken_pipe(script, arguments=()):
    """Run ``script`` with standard output a pipe whose reading end is closed."""
    reading_end, writing_end = os.pipe()
    os.close(reading_end)
    try:
        return subprocess.run(
            [sys.executable, "-c", script, *arguments],
            stdout=writing_end,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
    finally:
        os.close(writing_end)


@pytest.fixture
def publish_graph_runs(tmp_path):
    """Return each publish-graph run: its arguments, receipt and exit code."""
    directory = tmp_path / "graph"
    directory.mkdir()
    inventory = json.dumps({"publication_id": "pub-1"})
    (directory / "orrery.upload.json").write_text(inventory)
    return {
        "retry": (["--publication-id", "pub-1"], RETRY_RECEIPT, 0),
        "failed enqueue": (
            ["--publication-id", "pub-1", "--directory", str(directory)],
            FAILED_RECEIPT,
            1,
        ),
    }


PUBLISH_GRAPH_RUNS = pytest.mark.parametrize("run", ["retry", "failed enqueue"])


@PUBLISH_GRAPH_RUNS
@pytest.mark.parametrize("encoding", ["utf-8", "ascii:strict"])
def test_publish_graph_writes_its_receipt_as_one_line_of_json(
    run, encoding, publish_graph_runs
):
    arguments, receipt, exit_code = publish_graph_runs[run]
    completed = run_child(PUBLISH_GRAPH, arguments, encoding=encoding)
    assert completed.stdout == receipt.encode("ascii") + b"\n"
    assert completed.stderr == b""
    assert completed.returncode == exit_code


@PUBLISH_GRAPH_RUNS
def test_publish_graph_without_standard_output_writes_nothing(run, publish_graph_runs):
    arguments, receipt, exit_code = publish_graph_runs[run]
    completed = run_child(PUBLISH_GRAPH, arguments, redirections=">&-")
    printed = run_child(PRINT_AND_EXIT, [receipt, str(exit_code)], redirections=">&-")
    assert (completed.returncode, completed.stderr) == (exit_code, b"")
    assert (completed.returncode, completed.stderr) == (
        printed.returncode,
        printed.stderr,
    )


@PUBLISH_GRAPH_RUNS
def test_publish_graph_into_a_broken_pipe_fails_as_print_does(run, publish_graph_runs):
    arguments, receipt, exit_code = publish_graph_runs[run]
    completed = run_child_into_a_broken_pipe(PUBLISH_GRAPH, arguments)
    printed = run_child_into_a_broken_pipe(PRINT_AND_EXIT, [receipt, str(exit_code)])
    # Python reports the failed flush of standard output at exit, and exits 120.
    assert completed.returncode == 120
    assert b"BrokenPipeError" in completed.stderr
    assert (completed.returncode, completed.stderr) == (
        printed.returncode,
        printed.stderr,
    )


WARNINGS = pytest.mark.parametrize(
    ("script", "text"),
    [
        (EMITTER_WARNING, EMITTER_WARNING_TEXT),
        (INTERFACE_WARNING, INTERFACE_WARNING_TEXT),
    ],
    ids=["emitter", "interface"],
)


@WARNINGS
def test_a_warning_is_written_to_standard_error_in_its_encoding(script, text):
    completed = run_child(script)
    assert completed.stdout == b""
    assert completed.stderr == text.encode() + b"\n"
    assert completed.returncode == 0


@WARNINGS
def test_a_warning_that_standard_error_cannot_encode_is_escaped(script, text):
    completed = run_child(script, encoding="ascii")
    assert completed.stdout == b""
    assert completed.stderr == text.encode("ascii", "backslashreplace") + b"\n"
    assert completed.returncode == 0


@WARNINGS
def test_without_standard_error_a_warning_goes_to_standard_output(script, text):
    completed = run_child(script, redirections="2>&-")
    assert completed.stdout == text.encode() + b"\n"
    assert completed.returncode == 0


@WARNINGS
def test_without_either_stream_a_warning_is_dropped(script, text):
    completed = run_child(script, redirections=">&- 2>&-")
    assert completed.returncode == 0

"""Graph adapters only request service-side work and never claim lost data is queued."""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from microcosm_emitter.core.auth import shared_session
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.models import TelemetryRunRecord
from microcosm_emitter.host.errors import LocalServiceError
from microcosm_emitter.host.launcher import start_services
from microcosm_emitter.orrery import client as client_module
from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
from microcosm_emitter.orrery.service.queue import GraphPublicationQueue
from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun


def graph_files(tmp_path):
    directory = tmp_path / "graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"nodes":[]}')
    return directory


def test_public_client_enqueues_through_graph_only_service_without_run(tmp_path):
    directory = graph_files(tmp_path)
    path = tmp_path / "jobs.sqlite3"
    client = GraphPublicationClient.start(
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
    )
    assert client.available, getattr(client, "startup_error", None)
    try:
        inventory = client.inventory(
            directory,
            {"graph.orrery.json": "graph"},
            publication_id="independent-graph",
        )
        assert inventory is not None
        assert "run_id" not in inventory
        receipt = publish_graph(directory, inventory, client=client, wait_seconds=0)
        assert receipt["status"] == "pending"
        assert receipt["publication_id"] == "independent-graph"
        assert client.receipt("independent-graph")["status"] == "pending"
        assert client.retry("independent-graph")["status"] == "pending"
    finally:
        handle = client._handle
        client.close()
        assert handle.process.wait(timeout=20) == 0
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt("independent-graph")["status"] == "pending"
    finally:
        queue.close()
    engine = create_spool_engine(path)
    try:
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(TelemetryRunRecord))
                == 0
            )
    finally:
        engine.dispose()


def test_combined_host_keeps_graph_service_after_telemetry_completion(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    run = TelemetryRun("combined", "UK", "test")
    handle = start_services(
        {
            "telemetry": {
                "registration": run.as_registration(),
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
            },
            "orrery": {
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
                "publication_origin": "http://127.0.0.1:1",
            },
        }
    )
    graph = GraphPublicationClient(handle.client.for_module("orrery"))
    emitter = LocalTelemetryEmitter(
        run=run, transport=handle.client.for_module("telemetry")
    )
    try:
        emitter.transition_stage("hf_download")
        emitter.complete()
        handle.client.ping()
        directory = graph_files(tmp_path)
        inventory = graph.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        assert graph.enqueue(directory, inventory)["status"] == "pending"
        assert graph.receipt(inventory["publication_id"])["status"] == "pending"
    finally:
        graph.close()
        handle.client.ping()  # A module adapter cannot close the shared host.
        handle.close()
        assert handle.process.wait(timeout=20) == 0


def test_graph_adapter_never_imports_persistence_or_auth():
    tree = ast.parse(Path(client_module.__file__).read_text())
    imports = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not any(".service" in name or ".core" in name for name in imports)


def test_startup_failure_returns_unavailable_without_direct_queue(
    tmp_path, monkeypatch
):
    path = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        client_module, "start_service", Mock(side_effect=LocalServiceError("synthetic"))
    )
    client = GraphPublicationClient.start(spool_path=path)
    assert not client.available
    inventory = {"version": 1, "publication_id": "not-persisted", "files": []}
    assert client.enqueue(tmp_path, inventory) == {
        "version": 1,
        "publication_id": "not-persisted",
        "status": "failed",
        "error_code": "local_service_unavailable",
    }
    assert client.inventory(tmp_path, {}) is None
    assert not path.exists()


def test_request_failure_is_contained_without_claiming_pending(tmp_path):
    transport = Mock()
    transport.request.side_effect = OSError("synthetic private error")
    client = GraphPublicationClient(transport)
    inventory = {"version": 1, "publication_id": "not-acknowledged", "files": []}
    receipt = publish_graph(tmp_path, inventory, client=client, wait_seconds=0)
    assert receipt["status"] == "failed"
    assert "synthetic" not in receipt["error_code"]


def test_graph_timeout_failure_does_not_claim_durable_enqueue(tmp_path):
    from microcosm_emitter.orrery.constants import GRAPH_REQUEST_TIMEOUT_SECONDS

    transport = Mock()
    transport.request.side_effect = LocalServiceError("TimeoutError")
    client = GraphPublicationClient(transport)
    inventory = {"version": 1, "publication_id": "timed-out", "files": []}
    receipt = client.enqueue(tmp_path, inventory)
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "local_service_unavailable"
    assert transport.request.call_args.kwargs == {
        "timeout": GRAPH_REQUEST_TIMEOUT_SECONDS
    }


def test_modules_share_one_session_and_reject_conflicting_origins():
    services = {}
    first = shared_session(services, "https://collector.example")
    assert shared_session(services, "https://collector.example") is first
    with pytest.raises(ValueError, match="same collector origin"):
        shared_session(services, "https://different.example")


def test_retry_waits_for_a_new_attempt_via_receipt_requests(monkeypatch):
    pending = {
        "version": 1,
        "publication_id": "retry",
        "status": "pending",
        "error_code": "credential_missing",
        "attempts": 1,
    }
    tried = {**pending, "attempts": 2}
    transport = Mock()
    transport.request.side_effect = [{"receipt": pending}, {"receipt": tried}]
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
    client = GraphPublicationClient(transport)
    assert client.retry("retry", wait_seconds=1) == tried
    assert [call.args[0]["command"] for call in transport.request.call_args_list] == [
        "retry",
        "receipt",
    ]


@pytest.mark.parametrize("timeout", [None, 0.75])
def test_graph_start_uses_shared_default_and_preserves_override(
    monkeypatch, tmp_path, timeout
):
    from microcosm_emitter.host.constants import STARTUP_TIMEOUT

    launch = Mock(return_value=Mock())
    monkeypatch.setattr(client_module, "start_service", launch)
    kwargs = {} if timeout is None else {"startup_timeout": timeout}
    client = GraphPublicationClient.start(spool_path=tmp_path / "jobs", **kwargs)
    assert client.available
    assert STARTUP_TIMEOUT == 15.0
    assert launch.call_args.kwargs["startup_timeout"] == (
        STARTUP_TIMEOUT if timeout is None else timeout
    )
    assert client.startup_error is None
    client.close()


def test_graph_retains_safe_startup_failure_without_queue_fallback(
    monkeypatch, tmp_path
):
    from microcosm_emitter.host import errors

    failure = errors.ServiceStartupError("child_exit", exit_code=9)
    monkeypatch.setattr(client_module, "start_service", Mock(side_effect=failure))
    client = GraphPublicationClient.start(spool_path=tmp_path / "jobs")
    assert not client.available
    assert client.startup_error.reason == "child_exit"
    assert client.startup_error.exit_code == 9
    assert (
        client.enqueue(tmp_path, {"publication_id": "not-persisted"})["status"]
        == "failed"
    )
    assert not (tmp_path / "jobs").exists()


def test_graph_never_retains_raw_startup_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        client_module,
        "start_service",
        Mock(side_effect=OSError("Bearer hf_synthetic_private_payload")),
    )
    client = GraphPublicationClient.start(spool_path=tmp_path / "jobs")
    assert client.startup_error.reason == "startup_failure"
    assert str(client.startup_error) == "Service startup failed (startup_failure)."
    assert not hasattr(client.startup_error, "error_type")
    assert "hf_synthetic_private_payload" not in str(client.startup_error)
    assert client.startup_error.__cause__ is None
    assert client.startup_error.__context__ is None


def _telemetry_and_graph_modules(tmp_path, run):
    """Return a telemetry module for ``run`` and a graph module, sharing a context.

    Both use one event queue file in ``tmp_path`` and an unreachable collector.
    """
    import os

    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.orrery.service import module as graph_module
    from microcosm_emitter.telemetry.service.module import TelemetryModule

    context = ModuleContext(os.getpid())
    settings = {
        "spool_path": str(tmp_path / "events.sqlite3"),
        "development_collector_url": "http://127.0.0.1:1",
    }
    telemetry = TelemetryModule(
        {**settings, "registration": run.as_registration()}, context
    )
    graph = graph_module.OrreryModule(
        {**settings, "publication_origin": "http://127.0.0.1:1"}, context
    )
    return telemetry, graph


def _run_keeping_errors(runtime, errors):
    """Run ``runtime``, appending any exception it raises to ``errors``."""
    try:
        runtime.run()
    # Keeps any exception this thread's call raises, for the test to check.
    except Exception as error:  # noqa: BLE001
        errors.append(error)


def _wait_until_the_runtime_answers(client):
    """Ping through ``client`` until the runtime answers, for up to 3 seconds."""
    import time

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            client.ping()
            break
        except LocalServiceError:
            time.sleep(0.01)


def _hold_publication_inventory(monkeypatch, patched_module, entered, release):
    """Make ``publication_inventory`` on ``patched_module`` wait for ``release``.

    The replacement sets ``entered``, waits up to 5 seconds for ``release`` and
    then calls the graph queue's own ``publication_inventory``.
    """
    from microcosm_emitter.orrery.service import queue as graph_queue

    original_inventory = graph_queue.publication_inventory

    def delayed_inventory(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            error_message = "test did not release graph file work"
            raise TimeoutError(error_message)
        return original_inventory(*args, **kwargs)

    monkeypatch.setattr(patched_module, "publication_inventory", delayed_inventory)


def _graph_request_thread(graph_client, command, directory, inventory, results):
    """Return a thread that sends one ``command`` request and keeps its result."""
    import threading

    def graph_request():
        results.append(
            graph_client.inventory(directory, {"graph.orrery.json": "graph"})
            if command == "inventory"
            else graph_client.enqueue(directory, inventory)
        )

    return threading.Thread(target=graph_request)


@pytest.mark.parametrize("command", ["inventory", "enqueue"])
def test_graph_file_work_does_not_block_telemetry_or_control(
    tmp_path, monkeypatch, capsys, command
):
    """Graph file I/O must not consume another module's acknowledgement time."""
    import tempfile
    import threading

    from microcosm_emitter.host.client import SocketClient
    from microcosm_emitter.host.runtime import ServiceRuntime
    from microcosm_emitter.orrery.service import module as graph_module
    from microcosm_emitter.orrery.service import queue as graph_queue

    directory = graph_files(tmp_path)
    inventory = graph_queue.publication_inventory(
        directory, {"graph.orrery.json": "graph"}
    )
    run = TelemetryRun("concurrent-graph", "UK", "test")
    telemetry, graph = _telemetry_and_graph_modules(tmp_path, run)
    entered, release = threading.Event(), threading.Event()
    _hold_publication_inventory(
        monkeypatch,
        graph_module if command == "inventory" else graph_queue,
        entered,
        release,
    )
    errors, results = [], []
    with tempfile.TemporaryDirectory(prefix="pe-concurrency-", dir="/tmp") as raw:
        runtime = ServiceRuntime(
            Path(raw) / "s",
            modules={"telemetry": telemetry, "orrery": graph},
            is_parent_alive=lambda: True,
            tick_seconds=100,
            drain_seconds=0,
        )
        thread = threading.Thread(target=_run_keeping_errors, args=(runtime, errors))
        thread.start()
        client = SocketClient(runtime.socket_path)
        graph_client = GraphPublicationClient(client.for_module("orrery"))
        graph_thread = None
        try:
            _wait_until_the_runtime_answers(client)
            assert not errors
            graph_thread = _graph_request_thread(
                graph_client, command, directory, inventory, results
            )
            graph_thread.start()
            assert entered.wait(3)
            emitter = LocalTelemetryEmitter(
                run=run,
                transport=client.for_module("telemetry"),
                close_service_on_finish=False,
            )
            emitter.complete()
            assert telemetry.lifecycle.finished
            assert "could not be queued" not in capsys.readouterr().err
            client.ping()
            client.close()
            assert thread.is_alive()  # Shutdown waits for the accepted graph request.
            release.set()
            graph_thread.join(3)
            thread.join(3)
            assert not thread.is_alive()
            assert not graph_thread.is_alive()
            assert results[0] is not None
            if command == "enqueue":
                assert results[0]["status"] == "pending"
            assert not errors
        finally:
            release.set()
            runtime.stop()
            if graph_thread is not None:
                graph_thread.join(3)
            thread.join(3)

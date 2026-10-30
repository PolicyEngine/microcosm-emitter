"""Start the separately installed emitter from a build environment without it."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def main(emitter_command: str) -> None:
    """Start the emitter at ``emitter_command`` from a build environment and use it.

    The environment must lack the emitter package, and the interface package must
    have no dependencies and ship its protocol fixtures. The started emitter must
    run all three modules at the interface's version, accept telemetry, an Orrery
    hand-over and a run file, create its hand-over database, and exit with 0 once
    the client closes.
    """
    assert find_spec("microcosm_emitter") is None, "the build must not hold it"
    interface = distribution("microcosm-emitter-interface")
    assert not interface.requires, interface.requires
    from microcosm_emitter_interface import (
        EmitterClient,
        HuggingFaceSettings,
        OrrerySettings,
        RunFile,
        StageRunFiles,
        TelemetryRun,
        TelemetrySettings,
    )
    from microcosm_emitter_interface.protocol_fixtures import protocol_fixtures

    assert protocol_fixtures(1), "protocol fixtures must ship in the wheel"
    directory = Path.cwd() / "interface-graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    client = EmitterClient.start(
        telemetry=TelemetrySettings(
            run=TelemetryRun("interface-wheel", "UK", "installed"),
            development_collector_url="http://127.0.0.1:1",
        ),
        orrery=OrrerySettings(
            development_collector_url="http://127.0.0.1:1",
            publication_origin="http://127.0.0.1:1",
        ),
        # Without a credential (removed by check-installed.py) the module
        # queues hand-overs and never contacts the Hub.
        hugging_face=HuggingFaceSettings(),
        spool_path=Path.cwd() / "interface.sqlite3",
        command=emitter_command,
        # A clean install compiles dependency bytecode on first use.
        startup_timeout=15,
    )
    assert client.available, client.startup_error
    try:
        assert client.modules == {"telemetry", "orrery", "huggingface"}
        assert client.emitter_version == interface.version
        assert client.telemetry.transition_stage("compile")
        assert client.telemetry.complete()
        inventory = client.orrery_inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        receipt = client.hand_over_orrery_inputs(directory, inventory, wait_seconds=0)
        assert receipt["status"] == "pending", receipt
        progress = Path.cwd() / "progress.json"
        progress.write_text('{"stage":"created"}')
        staged = client.stage_run_files(
            StageRunFiles(
                repository="policyengine/installed-check",
                run_id="interface-wheel",
                files=(RunFile("progress.json", progress),),
                flush=True,
            )
        )
        assert staged["status"] == "recorded" and staged["pending_files"] == 1
        # The installed module keeps hand-overs in their own database, created
        # from the migrations shipped in the wheel, next to the spool database.
        assert (Path.cwd() / "interface.handovers.sqlite3").is_file()
    finally:
        process = client._process
        client.close()
    assert process is not None and process.process.wait(timeout=20) == 0


if __name__ == "__main__":
    main(sys.argv[1])

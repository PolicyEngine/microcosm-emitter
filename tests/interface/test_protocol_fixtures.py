"""This emitter accepts every protocol fixture the interface package ships.

Builds keep whichever emitter is installed, so every emitter release must
accept every protocol version that interface releases still in use send. The
fixtures are those messages; released fixtures are never edited, only added.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fake_hugging_face import FakeHuggingFaceApi

from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.host.runtime import ServiceRuntime
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter.orrery.service.module import OrreryModule
from microcosm_emitter.telemetry.service.module import TelemetryModule
from microcosm_emitter_interface.launcher import DEFAULT_COMMAND, start_emitter
from microcosm_emitter_interface.protocol_fixtures import (
    fixture_digests,
    materialize,
    protocol_fixtures,
)

#: SHA-256 of each released fixture file. A changed digest means a released
#: message changed shape: add a fixture or a protocol version instead.
RELEASED_FIXTURE_DIGESTS = {
    "protocol-v1/host-close.json": (
        "68fa81b4116852f91f3c0dab3716a1974a8150039fb2b238f79efc094d9b0f6f"
    ),
    "protocol-v1/huggingface-promote-build.json": (
        "069248aa33b24c937e18e00bc46dd1e39c6e06febedb5f7b5cab4c1e1fea9f07"
    ),
    "protocol-v1/huggingface-publish-release.json": (
        "12ccf6c9b613dae4e01709b745e8caacfbeb8f2ceeb6b8bfc8373af134de25c8"
    ),
    "protocol-v1/huggingface-withdraw-build.json": (
        "ad15257013d831c5830818668b222a12c0c8b53c5236ac0ff9733bbfd76c98be"
    ),
    "protocol-v1/huggingface-check-write-access.json": (
        "750b7eb8c7c9ea4112bb60560522d3b1591c6c9838239a5a54d72f90c095a631"
    ),
    "protocol-v1/huggingface-handover-receipt.json": (
        "e3d6735361da9326ce9c1aeb893d1c644a749e574ded8c1f2f2071f12e00ca09"
    ),
    "protocol-v1/huggingface-stage-dataset-bundle-with-collector.json": (
        "d676520e82c54e8c72ab2f54b73e8c29fdc02c295ac31ed23e970160c0cf9a26"
    ),
    "protocol-v1/huggingface-stage-dataset-bundle.json": (
        "6f576285b7a87607c47755ebee00ca051694b39455812a0068ebee2c27277728"
    ),
    "protocol-v1/huggingface-configuration.json": (
        "0ecea1a8e7ba6c4458c530b58fa90079207ee3f48da3e522479e9afde581551a"
    ),
    "protocol-v1/huggingface-read-back-run-files.json": (
        "ab643a81520cbf35fada4362d6bf08f63c84d45ed41d9ddf1a0a592855c91313"
    ),
    "protocol-v1/huggingface-run-files-receipt.json": (
        "acc37a2747fff6dc5ec134410f3c068b0cfb830e8703cdb273a83c690971315e"
    ),
    "protocol-v1/huggingface-stage-run-files-with-index.json": (
        "efe20fc318c6a576a37684884aff3497ac9aad093f4df81ca9b34a4ecbc0c0e7"
    ),
    "protocol-v1/huggingface-stage-run-files.json": (
        "e29d312aed140076789ab4f4c38bf7a9d11eadf1b446ea96d46a6f88ca7e4bd0"
    ),
    "protocol-v1/host-hello.json": (
        "36fcac377b69a9103b7c1cfd884d2ff667e030463edc9da349a379bb4868b927"
    ),
    "protocol-v1/host-launch.json": (
        "cb67b2cc3e8e709ca177c0f05b7b412fec4fbf7ffb1ae9b5028e3c3cbcb15a1c"
    ),
    "protocol-v1/host-message-envelope.json": (
        "21b5e6b22019cd82b2e4b57363c123ed2628bee86f4f4ee1f08c3ca7bac6cc12"
    ),
    "protocol-v1/host-ping.json": (
        "b4b6ba4668a29fcb79160a245f3b92b0191cd98e9a611a2713f65523e40434a4"
    ),
    "protocol-v1/host-query-envelope.json": (
        "dd364ff5c91f1a81778245a8453c1b0eee8434c9824898f36327204e032d22e8"
    ),
    "protocol-v1/orrery-configuration.json": (
        "956b5e1ca4c65a9fc0282f648b45d00ae697ac72e50147269e365a52d16f141a"
    ),
    "protocol-v1/orrery-enqueue.json": (
        "e70c33e089b7361dc01e0448dd34458c8ca8c606ea8c552af191c9b08cb11c5b"
    ),
    "protocol-v1/orrery-inventory.json": (
        "8cba4ec76bec8db21f1e158bf5da4eef23ca799f6730012051280d33763092f5"
    ),
    "protocol-v1/orrery-receipt.json": (
        "cbc7ed6d1776af8082d6def5f920d547400c7da073febea89364150ccd4e7a4b"
    ),
    "protocol-v1/orrery-retry.json": (
        "acfa79ba0221ff3786e61cdad699124e81cf825cc4b1364895a3679e839ade49"
    ),
    "protocol-v1/telemetry-calibration-progress.json": (
        "52c2396dd12f9e0cb65e429139d0bd1f2fa6e5adf9c007fd513161de560775c4"
    ),
    "protocol-v1/telemetry-complete.json": (
        "8f32aa105366fc544a3dd85d3e330c8602feb2f770bb196d49d09a4126747a05"
    ),
    "protocol-v1/telemetry-configuration.json": (
        "fd2ae2786543b8d9239f4f5a6604440daada9ab89ec974abc858b4847c3a563b"
    ),
    "protocol-v1/telemetry-emit.json": (
        "0ae388f58fae80267391360c30b21bf392072eea7158dda465630911e3d919c3"
    ),
    "protocol-v1/telemetry-fail-without-code.json": (
        "ad55024a6bf0fae3583245b74b83671ea652dbb5ad48763d2e1e341c116e5fdb"
    ),
    "protocol-v1/telemetry-fail.json": (
        "f509a443a361dfa5133fa32667b88a2e6be8f0ef29f8facfc03136e1af215b56"
    ),
    "protocol-v1/telemetry-progress.json": (
        "fcb9f3b69b2c4e941b9a7f4af300a87781861d6b1b69acdeb1ba2f3fb7d52166"
    ),
    "protocol-v1/telemetry-refusal.json": (
        "33c6bc9750b9e342a8a86e7cc9c25c23a76a03419b6bdb1690bfb9ec3353c82c"
    ),
    "protocol-v1/telemetry-stage.json": (
        "083a6a88e30092b45a75911d8cd80e7f8079a7715d2a5870215203ec15c642ec"
    ),
    "protocol-v1/telemetry-transition-calibration-progress.json": (
        "9236f9bc9c0cf771fc67af01f1b99606be28a8c49d142709aeb6f3cad1bc46da"
    ),
    "protocol-v1/telemetry-transition-stage.json": (
        "50fcb5f0379579625db58daba44aab2d1a63c73c4d54738dfdf65d7b93d8c783"
    ),
}


def fixtures_of(module: str, kind: str):
    return sorted(
        (
            fixture
            for fixture in protocol_fixtures()
            if fixture.module == module and fixture.kind == kind
        ),
        key=lambda fixture: (fixture.sequence, fixture.name),
    )


def configuration_of(module: str, tmp_path: Path) -> dict:
    (fixture,) = fixtures_of(module, "configuration")
    return materialize(
        fixture, directory=tmp_path, spool_path=tmp_path / "events.sqlite3"
    )


def test_every_fixture_uses_a_protocol_version_this_emitter_accepts():
    fixtures = protocol_fixtures()
    assert fixtures
    assert {fixture.protocol_version for fixture in fixtures} <= set(
        SUPPORTED_PROTOCOL_VERSIONS
    )


def test_released_fixtures_are_unchanged():
    assert fixture_digests() == RELEASED_FIXTURE_DIGESTS


@pytest.mark.parametrize(
    "fixture",
    fixtures_of("telemetry", "message"),
    ids=lambda fixture: fixture.name,
)
def test_telemetry_module_records_each_message_fixture(tmp_path, fixture):
    module = TelemetryModule(
        configuration_of("telemetry", tmp_path), ModuleContext(os.getpid())
    )
    module.initialize()
    try:
        registration = module.registration
        before = module.spool.batch(registration["run_id"], registration["producer_id"])
        module.handle_message(
            materialize(fixture, directory=tmp_path, spool_path=tmp_path)
        )
        after = module.spool.batch(registration["run_id"], registration["producer_id"])
        assert len(after) > len(before)
    finally:
        module.close(time.monotonic())


def test_orrery_module_answers_each_query_fixture_in_order(tmp_path):
    module = OrreryModule(
        configuration_of("orrery", tmp_path), ModuleContext(os.getpid())
    )
    module.initialize()
    directory = tmp_path / "inputs"
    try:
        for fixture in fixtures_of("orrery", "query"):
            directory.mkdir(exist_ok=True)
            for name, text in fixture.files.items():
                (directory / name).write_text(text)
            response = module.query(
                materialize(fixture, directory=directory, spool_path=tmp_path)
            )
            assert set(fixture.response_keys) <= set(response), fixture.name
            receipt = response.get("receipt")
            if receipt is not None:
                assert receipt["status"] == "pending", fixture.name
    finally:
        module.close(time.monotonic())


def test_hugging_face_module_answers_each_query_fixture_in_order(tmp_path):
    hub = FakeHuggingFaceApi(whoami={"auth": {"accessToken": {"role": "write"}}})
    hub.create_repository("policyengine/fixture-staging")
    hub.create_repository("policyengine/fixture-private")
    module = HuggingFaceModule(
        configuration_of("huggingface", tmp_path),
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
    )
    module.initialize()
    directory = tmp_path / "inputs"
    directory.mkdir()
    try:
        for fixture in fixtures_of("huggingface", "query"):
            for name, text in fixture.files.items():
                (directory / name).write_text(text)
            response = module.query(
                materialize(fixture, directory=directory, spool_path=tmp_path)
            )
            assert set(fixture.response_keys) <= set(response), fixture.name
            if response.get("receipt") is not None:
                assert response["receipt"]["status"] in {"recorded", "pending"}, (
                    fixture.name
                )
            if "read_back" in response:
                assert response["read_back"]["status"] in {"pending", "passed"}
            if "access" in response:
                assert response["access"]["status"] == "checked"
    finally:
        module.close(time.monotonic())


def test_installed_command_answers_the_hello_fixture(tmp_path):
    """The answer has at least the recorded keys; a newer emitter may add more."""
    (fixture,) = fixtures_of("host", "hello")
    process = start_emitter(
        {"orrery": configuration_of("orrery", tmp_path)},
        command=shutil.which(DEFAULT_COMMAND),
    )
    try:
        assert set(fixture.response_keys) <= set(process.description)
    finally:
        process.close()
    assert process.process.wait(timeout=20) == 0


def raw_request(socket_path: Path, body: dict, *, timeout: float = 5.0) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(timeout)
        peer.connect(str(socket_path))
        peer.sendall(json.dumps(body).encode() + b"\n")
        response = bytearray()
        while not response.endswith(b"\n"):
            part = peer.recv(65_536)
            if not part:
                break
            response.extend(part)
    return bytes(response)


@pytest.fixture
def socket_directory():
    # Short paths are necessary for macOS's Unix-socket path limit.
    directory = Path(tempfile.mkdtemp(prefix="pe-fixture-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_installed_command_starts_from_the_launch_fixture(tmp_path, socket_directory):
    """The recorded arguments and standard input start this emitter's host."""
    (launch,) = fixtures_of("host", "launch")
    socket_path = socket_directory / "service.sock"
    launched = materialize(
        launch,
        directory=tmp_path,
        spool_path=tmp_path / "events.sqlite3",
        socket_path=socket_path,
        parent_pid=os.getpid(),
    )
    process = subprocess.Popen(
        [shutil.which(DEFAULT_COMMAND), *launched["arguments"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.stdin.write(json.dumps(launched["standard_input"]).encode())
        process.stdin.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, "the host refused the launch fixture"
            try:
                if raw_request(socket_path, {"action": "ping"}) == b"ok\n":
                    break
            except OSError:
                time.sleep(0.05)
        hello = json.loads(raw_request(socket_path, {"action": "hello"}))
        assert hello["modules"] == sorted(launched["standard_input"])
        assert raw_request(socket_path, {"action": "close"}) == b"ok\n"
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


class RecordingModule:
    """Records the messages and queries the host routes to it."""

    def __init__(self):
        self.messages: list[dict] = []
        self.queries: list[dict] = []

    def initialize(self):
        pass

    def handle_message(self, message):
        self.messages.append(dict(message))

    def query(self, message):
        self.queries.append(dict(message))
        return {"receipt": None}

    def tick(self, now):
        pass

    def parent_exited(self):
        pass

    def close(self, deadline):
        pass


def test_host_answers_each_envelope_fixture(socket_directory):
    """The host parses each recorded envelope and routes it to its module."""
    modules = {"orrery": RecordingModule(), "telemetry": RecordingModule()}
    socket_path = socket_directory / "service.sock"
    runtime = ServiceRuntime(
        socket_path,
        modules=modules,
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
        description={"modules": sorted(modules)},
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        envelopes = fixtures_of("host", "envelope")
        assert envelopes
        for fixture in envelopes:
            response = raw_request(socket_path, fixture.body)
            if fixture.response == "acknowledgement":
                assert response == b"ok\n", fixture.name
            else:
                assert fixture.response == "answer", fixture.name
                assert isinstance(json.loads(response), dict), fixture.name
            body = fixture.body
            if body["action"] == "message":
                assert modules[body["module"]].messages == [body["message"]]
            elif body["action"] == "query":
                assert modules[body["module"]].queries == [body["message"]]
        assert envelopes[-1].body == {"action": "close"}
        thread.join(timeout=10)
        assert not thread.is_alive(), "the close envelope did not stop the host"
    finally:
        runtime.stop()
        thread.join(timeout=10)

"""A loopback fake collector, and the Hugging Face module wired to it."""

import os
from types import SimpleNamespace

import pytest
from fake_collector import FakeCollector
from fake_hugging_face import PRIVATE_REPOSITORY, FakeHuggingFaceApi


class ManualClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


def new_module(
    tmp_path, collector, *, background_workers=False, clock=None, **configuration
):
    """Build the module and its in-memory Hub, not yet initialized.

    ``clock`` is the module's clock; by default a manual one the test moves.
    """
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    hub = FakeHuggingFaceApi(
        whoami={"auth": {"accessToken": {"role": "write"}}, "name": "fixture"}
    )
    hub.create_repository(PRIVATE_REPOSITORY, {"README.md": b"private fixture"})
    clock = ManualClock() if clock is None else clock
    available_api = {"value": hub}
    module = HuggingFaceModule(
        {
            "handover_database_path": str(tmp_path / "events.handovers.sqlite3"),
            **configuration,
        },
        ModuleContext(os.getpid()),
        api_factory=lambda: available_api["value"],
        clock=clock,
        collector_credential=lambda: "hf_synthetic",
        # Tests that drive the module by hand run the delivery cycles on
        # their own thread; the others run the module's own workers.
        background_workers=background_workers,
        worker_interval_seconds=0.01,
    )
    return SimpleNamespace(
        module=module,
        clock=clock,
        hub=hub,
        collector=collector,
        tmp_path=tmp_path,
        remove_credential=lambda: available_api.update(value=None),
        restore_credential=lambda: available_api.update(value=hub),
    )


def start_module(tmp_path, collector, **options):
    controls = new_module(tmp_path, collector, **options)
    controls.module.initialize()
    return controls


@pytest.fixture
def submitting_module(tmp_path, collector):
    """Start the module with collector submissions switched on, as for development."""
    controls = start_module(
        tmp_path,
        collector,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    try:
        yield controls
    finally:
        controls.module.close(0)


@pytest.fixture
def default_module(tmp_path, collector):
    """Start the module as production starts it: collector submissions off."""
    controls = start_module(tmp_path, collector)
    try:
        yield controls
    finally:
        controls.module.close(0)


@pytest.fixture
def module_factory(tmp_path, collector):
    """Build modules wired to this test's collector.

    ``new`` leaves the module uninitialized (for a host runtime to start),
    ``start`` initializes it.
    """
    return SimpleNamespace(
        new=lambda **options: new_module(tmp_path, collector, **options),
        start=lambda **options: start_module(tmp_path, collector, **options),
    )

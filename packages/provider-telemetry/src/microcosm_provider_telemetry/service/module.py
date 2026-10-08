"""Telemetry lifecycle and delivery, independent of the local socket host."""

import math
import threading
import time

from microcosm_provider_client.contracts import JsonObject, ModuleContext

from microcosm_provider_telemetry.protocol import UNEXPECTED_PROCESS_EXIT_MESSAGE
from microcosm_provider_telemetry.requests import COMMAND_FAIL
from microcosm_provider_telemetry.service.collector import CollectorDelivery
from microcosm_provider_telemetry.service.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    DRAIN_RETRY_SECONDS,
    FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT,
    MINIMUM_HEARTBEAT_SECONDS,
)
from microcosm_provider_telemetry.service.lifecycle import (
    LifecycleState,
    heartbeat_event,
    process_request,
    started_event,
)
from microcosm_provider_telemetry.service.resources import ProcessTreeSampler
from microcosm_provider_telemetry.service.spool import EventSpool


class TelemetryModule:
    """Persist before acknowledging; authenticate and deliver on the worker thread."""

    def __init__(self, configuration: JsonObject, context: ModuleContext) -> None:
        self.configuration = configuration
        self.context = context
        self.spool: EventSpool | None = None
        self.delivery: CollectorDelivery | None = None
        self._lock = threading.RLock()
        self.lifecycle = LifecycleState()

    def initialize(self) -> None:
        self.registration = dict(self.configuration["registration"])
        heartbeat = float(
            self.configuration.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)
        )
        if not math.isfinite(heartbeat):
            raise ValueError("heartbeat interval must be finite")
        self.heartbeat_seconds = max(MINIMUM_HEARTBEAT_SECONDS, heartbeat)
        self._next_heartbeat = time.monotonic() + self.heartbeat_seconds
        self.sampler = ProcessTreeSampler(self.context.parent_pid)
        self.spool = EventSpool(self.configuration["spool_path"])
        try:
            self.delivery = CollectorDelivery(
                self.spool,
                development_collector_url=self.configuration.get(
                    "development_collector_url"
                ),
            )
            self.spool.register(self.registration)
            self.spool.append(
                self.registration,
                started_event(self.configuration.get("identity", {})),
                resources=self.sampler.sample(),
            )
        except Exception:
            self.spool.close()
            raise

    def handle_message(self, message: JsonObject) -> None:
        with self._lock:
            state, events = process_request(self.lifecycle, message)
            if events:
                self.spool.append_many(
                    self.registration, events, resources=self.sampler.sample()
                )
            # Never advance stage state if a transaction fails.
            self.lifecycle = state

    def tick(self, now: float) -> None:
        with self._lock:
            resources = self.sampler.sample()
            if not self.lifecycle.finished and now >= self._next_heartbeat:
                self.spool.append(
                    self.registration,
                    heartbeat_event(self.lifecycle),
                    resources=resources,
                )
                self._next_heartbeat = now + self.heartbeat_seconds
        # Network I/O must never hold the lock used by local message handling.
        self.spool.prune_if_due()
        self.delivery.flush_once()

    def parent_exited(self) -> None:
        with self._lock:
            if not self.lifecycle.finished:
                self.handle_message(
                    {
                        "command": COMMAND_FAIL,
                        "message": UNEXPECTED_PROCESS_EXIT_MESSAGE,
                        "failure_class": FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT,
                        "failed_during": self.lifecycle.last_stage,
                    }
                )

    def close(self, deadline: float) -> None:
        try:
            if self.delivery is not None:
                while time.monotonic() < deadline and self.spool.has_deliverable():
                    if not self.delivery.flush_once():
                        time.sleep(
                            min(
                                DRAIN_RETRY_SECONDS, max(0, deadline - time.monotonic())
                            )
                        )
        finally:
            if self.spool is not None:
                self.spool.close()

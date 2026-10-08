"""Telemetry lifecycle and delivery, independent of the local socket host."""

import math
import threading
import time
from collections.abc import Mapping

from policyengine_local_service.contracts import JsonObject, ModuleContext

from policyengine_telemetry.protocol import (
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_RUN,
    STAGE_CREATED,
    STAGE_FAILED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROGRESS,
    UNEXPECTED_PROCESS_EXIT_MESSAGE,
)
from policyengine_telemetry.service.collector import CollectorDelivery
from policyengine_telemetry.service.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    DRAIN_RETRY_SECONDS,
    FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT,
    MINIMUM_HEARTBEAT_SECONDS,
)
from policyengine_telemetry.service.resources import ProcessTreeSampler
from policyengine_telemetry.service.spool import EventSpool
from policyengine_telemetry.service.timestamps import utc_now


class TelemetryModule:
    """Persist before acknowledging; authenticate and deliver on the worker thread."""

    def __init__(self, configuration: JsonObject, context: ModuleContext) -> None:
        self.configuration = configuration
        self.context = context
        self.spool: EventSpool | None = None
        self.delivery: CollectorDelivery | None = None
        self._lock = threading.RLock()
        self._finished = False
        self._last_stage = STAGE_CREATED

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
        self.delivery = CollectorDelivery(
            self.spool,
            development_collector_url=self.configuration.get(
                "development_collector_url"
            ),
        )
        self.spool.register(self.registration)

    def handle_message(self, message: JsonObject) -> None:
        if message.get("event_type") not in {
            "run",
            "stage",
            "progress",
            "calibration",
            "heartbeat",
        }:
            raise ValueError("unsupported telemetry event type")
        if message.get("status") not in {"started", "progress", "completed", "failed"}:
            raise ValueError("unsupported telemetry status")
        if not isinstance(message.get("details", {}), Mapping):
            raise ValueError("telemetry details must be an object")
        with self._lock:
            self.spool.append(
                self.registration, message, resources=self.sampler.sample()
            )
            if message.get("event_type") == EVENT_TYPE_RUN and message.get(
                "status"
            ) in {STATUS_COMPLETED, STATUS_FAILED}:
                self._finished = True
            elif isinstance(message.get("stage_id"), str):
                self._last_stage = message["stage_id"]

    def tick(self, now: float) -> None:
        with self._lock:
            resources = self.sampler.sample()
            if not self._finished and now >= self._next_heartbeat:
                self.spool.append(
                    self.registration,
                    {
                        "timestamp": utc_now(),
                        "event_type": EVENT_TYPE_HEARTBEAT,
                        "stage_id": self._last_stage,
                        "status": STATUS_PROGRESS,
                    },
                    resources=resources,
                )
                self._next_heartbeat = now + self.heartbeat_seconds
        # Network I/O must never hold the lock used by local message handling.
        self.delivery.flush_once()

    def parent_exited(self) -> None:
        with self._lock:
            if not self._finished:
                self.handle_message(
                    {
                        "timestamp": utc_now(),
                        "event_type": EVENT_TYPE_RUN,
                        "stage_id": STAGE_FAILED,
                        "status": STATUS_FAILED,
                        "message": UNEXPECTED_PROCESS_EXIT_MESSAGE,
                        "details": {
                            "failure_class": FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT,
                            "failed_during": self._last_stage,
                        },
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

"""Telemetry lifecycle and delivery, independent of the local socket host."""

import math
import threading
import time

from microcosm_emitter.core.auth import shared_session
from microcosm_emitter.core.collector_origin import (
    COLLECTOR_ORIGIN_SETTING,
    DEVELOPMENT_COLLECTOR_URL_SETTING,
    configured_collector_origin,
)
from microcosm_emitter.core.constants import (
    DEFAULT_HEARTBEAT_SECONDS,
    DRAIN_RETRY_SECONDS,
    FAILURE_CLASS_UNEXPECTED_PROCESS_EXIT,
    MINIMUM_HEARTBEAT_SECONDS,
)
from microcosm_emitter.host.contracts import JsonObject, ModuleContext
from microcosm_emitter.telemetry.protocol import UNEXPECTED_PROCESS_EXIT_MESSAGE
from microcosm_emitter.telemetry.requests import COMMAND_FAIL
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.lifecycle import (
    LifecycleState,
    heartbeat_event,
    process_request,
    started_event,
)
from microcosm_emitter.telemetry.service.resources import ProcessTreeSampler
from microcosm_emitter.telemetry.service.spool import EventSpool


class TelemetryModule:
    """Persist before acknowledging; authenticate and deliver on the worker thread."""

    def __init__(self, configuration: JsonObject, context: ModuleContext) -> None:
        """Hold the configuration and context; ``initialize`` opens the event queue."""
        self.configuration = configuration
        self.context = context
        self.spool: EventSpool | None = None
        self.delivery: CollectorDelivery | None = None
        self._lock = threading.RLock()
        self.lifecycle = LifecycleState()

    def initialize(self) -> None:
        """Read the settings, open the event queue and record that the run started.

        An untrusted collector origin or an unusable given collector token is
        refused before the event queue is opened, and a heartbeat interval that
        is not finite raises ``ValueError``. The run is registered and its
        started event appended, with a resource sample.
        """
        self.registration = dict(self.configuration["registration"])
        heartbeat = float(
            self.configuration.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)
        )
        if not math.isfinite(heartbeat):
            error_message = "heartbeat interval must be finite"
            raise ValueError(error_message)
        self.heartbeat_seconds = max(MINIMUM_HEARTBEAT_SECONDS, heartbeat)
        self._next_heartbeat = time.monotonic() + self.heartbeat_seconds
        self.sampler = ProcessTreeSampler(self.context.parent_pid)
        # Refuse an untrusted origin, or an unusable given collector token,
        # before opening the spool.
        session = shared_session(
            self.context.services, configured_collector_origin(self.configuration)
        )
        self.spool = EventSpool(self.configuration["spool_path"])
        try:
            self.delivery = CollectorDelivery(
                self.spool,
                session=session,
                # Empty settings count as unset, as configured_collector_origin
                # treats them, so the delivery and the session agree.
                development_collector_url=(
                    self.configuration.get(DEVELOPMENT_COLLECTOR_URL_SETTING) or None
                ),
                collector_origin=(
                    self.configuration.get(COLLECTOR_ORIGIN_SETTING) or None
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
        """Turn one build request into events and append them to the event queue.

        The lifecycle state changes only once the events are stored.
        """
        with self._lock:
            state, events = process_request(self.lifecycle, message)
            if events:
                self.spool.append_many(
                    self.registration, events, resources=self.sampler.sample()
                )
            # Never advance stage state if a transaction fails.
            self.lifecycle = state

    def tick(self, now: float) -> None:
        """Do the periodic work: a heartbeat, pruning and one delivery pass.

        ``now`` is a ``time.monotonic()`` reading. The heartbeat is appended only
        when it is due and the run has not finished, and pruning runs only when
        it is due.
        """
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
        """Fail an unfinished run because the build process exited unexpectedly."""
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
        """Deliver queued events until ``deadline``, then close the event queue.

        ``deadline`` is a ``time.monotonic()`` value. Delivery stops earlier when
        nothing is left to deliver or when this emitter cannot deliver anything.
        """
        try:
            if self.delivery is not None:
                while time.monotonic() < deadline and self.spool.has_deliverable():
                    if not self.delivery.flush_once():
                        if self.delivery.waiting_for_credential:
                            # Nothing more can be delivered from here.
                            break
                        time.sleep(
                            min(
                                DRAIN_RETRY_SECONDS, max(0, deadline - time.monotonic())
                            )
                        )
        finally:
            if self.spool is not None:
                self.spool.close()

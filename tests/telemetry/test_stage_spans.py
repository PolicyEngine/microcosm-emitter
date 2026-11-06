"""Every stage start the emitter sees gets one end, with its parent and timing."""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.clocks import ClockReading
from microcosm_emitter.telemetry.service.lifecycle import (
    LifecycleState,
    process_request,
    started_event,
)
from microcosm_emitter.telemetry.service.module import TelemetryModule

START = datetime(2026, 10, 24, 8, 31, tzinfo=UTC)
EMITTER_KEYS = ("reading", "sleep", "environment", "stage_span", "outer")


def clock(awake, asleep=0.0):
    """Return a reading ``awake`` seconds in, ``asleep`` seconds slept so far."""
    return ClockReading(
        awake=awake,
        total=awake + asleep,
        wall=START + timedelta(seconds=awake + asleep),
    )


def apply(requests, state=None):
    """Apply ``(request, clock reading)`` pairs; return the state and the events."""
    state = LifecycleState() if state is None else state
    recorded = []
    for request, reading in requests:
        state, events = process_request(state, request, reading)
        recorded.extend(events)
    return state, recorded


def stage(stage_id, status="started", **details):
    return {
        "command": "stage",
        "stage_id": stage_id,
        "status": status,
        "details": details,
    }


def transition(stage_id, status="running"):
    return {"command": "transition_stage", "stage_id": stage_id, "status": status}


def stage_events(events):
    """Each stage event's stage, status and span, in order."""
    return [
        (event["stage_id"], event["status"], event["details"].get("stage_span"))
        for event in events
        if event["event_type"] == "stage"
    ]


def test_a_stage_start_records_its_parent_and_occurrence():
    _, events = apply(
        [
            (stage("build"), clock(0)),
            (stage("score"), clock(10)),
            (stage("score", "completed"), clock(20)),
            (stage("score"), clock(30)),
        ]
    )
    assert stage_events(events) == [
        ("build", "started", {"parent_stage_id": None, "occurrence": 1}),
        ("score", "started", {"parent_stage_id": "build", "occurrence": 1}),
        (
            "score",
            "completed",
            {
                "parent_stage_id": "build",
                "occurrence": 1,
                "awake_seconds": 10.0,
                "asleep_seconds": 0.0,
                "closed_by": None,
            },
        ),
        ("score", "started", {"parent_stage_id": "build", "occurrence": 2}),
    ]


def test_a_stage_end_records_its_awake_and_asleep_seconds():
    _, events = apply(
        [
            (stage("score"), clock(0)),
            (stage("score", "completed"), clock(100, asleep=3600)),
        ]
    )
    span = events[-1]["details"].get("stage_span") or {}
    assert (span.get("awake_seconds"), span.get("asleep_seconds")) == (100.0, 3600.0)


def test_without_clock_readings_an_end_has_no_durations():
    _, events = apply([(stage("score"), None), (stage("score", "completed"), None)])
    span = events[-1]["details"].get("stage_span") or {}
    assert span.get("occurrence") == 1
    assert (span.get("awake_seconds"), span.get("asleep_seconds")) == (None, None)


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_ending_a_stage_first_ends_the_stages_opened_inside_it(status):
    state, events = apply(
        [
            (stage("build"), clock(0)),
            (stage("score"), clock(1)),
            (stage("check"), clock(2)),
            (stage("build", status), clock(3)),
        ]
    )
    ends = [
        (stage_id, end_status, (span or {}).get("closed_by"))
        for stage_id, end_status, span in stage_events(events)[3:]
    ]
    assert ends == [
        ("check", status, "enclosing_stage_ended"),
        ("score", status, "enclosing_stage_ended"),
        ("build", status, None),
    ]
    assert state.open_stages == ()


def test_completing_the_run_ends_every_open_stage_innermost_first():
    state, events = apply(
        [
            (stage("build"), clock(0)),
            (transition("score"), clock(1)),
            ({"command": "complete"}, clock(5)),
        ]
    )
    assert [
        (event["event_type"], event["stage_id"], event["status"]) for event in events
    ][-3:] == [
        ("stage", "score", "completed"),
        ("stage", "build", "completed"),
        ("run", "complete", "completed"),
    ]
    assert [
        (span or {}).get("closed_by") for _, _, span in stage_events(events)[-2:]
    ] == ["run_completed", "run_completed"]
    assert state.open_stages == ()
    assert state.transition_stage is None


def test_a_failure_ends_every_open_stage_and_the_innermost_carries_it():
    _, events = apply(
        [
            (stage("build"), clock(0)),
            (transition("score"), clock(1)),
            (
                {
                    "command": "fail",
                    "message": "Synthetic failure.",
                    "error_type": "RuntimeError",
                },
                clock(2),
            ),
        ]
    )
    score_end, build_end, run_end = events[-3:]
    assert (score_end["stage_id"], score_end["status"]) == ("score", "failed")
    assert score_end["message"] == "Synthetic failure."
    assert score_end["details"].get("error_type") == "RuntimeError"
    assert (score_end["details"].get("stage_span") or {}).get("closed_by") == (
        "run_failed"
    )
    assert (build_end["stage_id"], build_end["status"]) == ("build", "failed")
    assert build_end["message"] is None
    assert (build_end["details"].get("stage_span") or {}).get("closed_by") == (
        "run_failed"
    )
    assert (run_end["event_type"], run_end["status"]) == ("run", "failed")
    assert run_end["details"]["failed_during"] == "score"


def test_a_refusal_ends_every_open_stage_as_completed():
    _, events = apply(
        [
            (stage("build"), clock(0)),
            (stage("preflight"), clock(1)),
            (
                {
                    "command": "block",
                    "phase": "preflight",
                    "blocking_gate_ids": ["fixture-check"],
                },
                clock(2),
            ),
        ]
    )
    assert [
        (stage_id, status, (span or {}).get("closed_by"))
        for stage_id, status, span in stage_events(events)[-2:]
    ] == [
        ("preflight", "completed", "run_blocked"),
        ("build", "completed", "run_blocked"),
    ]
    assert events[-1]["status"] == "blocked"


def test_moving_to_the_next_transition_stage_says_why_the_last_one_ended():
    _, events = apply(
        [(transition("load"), clock(0)), (transition("compile"), clock(4))]
    )
    assert stage_events(events) == [
        ("load", "started", {"parent_stage_id": None, "occurrence": 1}),
        (
            "load",
            "completed",
            {
                "parent_stage_id": None,
                "occurrence": 1,
                "awake_seconds": 4.0,
                "asleep_seconds": 0.0,
                "closed_by": "next_stage_started",
            },
        ),
        ("compile", "started", {"parent_stage_id": None, "occurrence": 1}),
    ]


def test_a_stage_inside_a_transition_stage_ends_with_it():
    state, events = apply(
        [
            (transition("score"), clock(0)),
            (stage("check"), clock(1)),
            (transition("score", "completed"), clock(2)),
        ]
    )
    assert [
        (stage_id, status, (span or {}).get("closed_by"))
        for stage_id, status, span in stage_events(events)[2:]
    ] == [
        ("check", "completed", "enclosing_stage_ended"),
        ("score", "completed", None),
    ]
    assert state.open_stages == ()
    assert state.transition_stage is None


def test_an_end_without_a_start_has_no_span():
    _, events = apply([(stage("score", "completed"), clock(0))])
    assert "stage_span" not in events[-1]["details"]


def test_a_stage_progress_event_has_no_span():
    _, events = apply(
        [(stage("score"), clock(0)), (stage("score", "progress"), clock(1))]
    )
    assert "stage_span" not in events[-1]["details"]


def test_at_most_32_open_stages_are_tracked():
    state, events = apply(
        [(stage(f"stage-{index}"), clock(index)) for index in range(33)]
    )
    assert len(state.open_stages) == 32
    assert "stage_span" in events[31]["details"]
    assert "stage_span" not in events[32]["details"]


@pytest.mark.parametrize("key", EMITTER_KEYS)
@pytest.mark.parametrize(
    "request_for",
    [
        lambda details: stage("score", **details),
        lambda details: {
            "command": "progress",
            "stage_id": "score",
            "done": 1,
            "total": 2,
            "details": details,
        },
        lambda details: {
            "command": "emit",
            "event_type": "heartbeat",
            "status": "progress",
            "details": details,
        },
    ],
    ids=["stage", "progress", "emit"],
)
def test_build_details_may_not_use_the_emitters_own_keys(key, request_for):
    state = LifecycleState()
    with pytest.raises(ValueError, match="emitter's own"):
        process_request(state, request_for({key: 1}), clock(0))


def large_details():
    """Return details far beyond the 8 KiB limit, whose values the limit truncates."""
    return {f"large_{index}": "x" * 2000 for index in range(10)}


def details_bytes(details):
    return len(json.dumps(details, separators=(",", ":")).encode())


def test_the_span_survives_when_the_builds_details_are_compacted():
    _, events = apply([(stage("score", **large_details()), clock(0))])
    details = events[-1]["details"]
    assert details["telemetry_details_truncated"]
    assert details.get("stage_span") == {"parent_stage_id": None, "occurrence": 1}


@pytest.mark.parametrize(
    ("requests", "emitter_key"),
    [
        ([(stage("x" * 192, **large_details()), clock(0))], "stage_span"),
        (
            [
                (stage("x" * 192), clock(0)),
                (stage("y" * 192), clock(1)),
                (stage("y" * 192, "completed", **large_details()), clock(2)),
            ],
            "stage_span",
        ),
        (
            [
                (
                    {
                        "command": "progress",
                        "stage_id": "score",
                        "done": 1,
                        "total": 2,
                        "unit": "u" * 200,
                        "outer": {"done": 1, "total": 2, "unit": "p" * 200},
                        "details": large_details(),
                    },
                    clock(0),
                )
            ],
            "outer",
        ),
    ],
    ids=["stage start", "stage end", "progress"],
)
def test_the_emitters_keys_keep_details_within_the_limit(requests, emitter_key):
    _, events = apply(requests)
    details = events[-1]["details"]
    assert emitter_key in details
    assert details["telemetry_details_truncated"]
    assert details_bytes(details) <= 8192


def test_the_machine_description_keeps_the_run_start_within_the_limit():
    event = started_event(
        {"identity": large_details()},
        {"kind": "local", "cpu_model": "m" * 200, "os": "Linux"},
    )
    assert event["details"]["environment"]["cpu_model"] == "m" * 200
    assert details_bytes(event["details"]) <= 8192


def test_progress_records_the_outer_counter():
    _, events = apply(
        [
            (
                {
                    "command": "progress",
                    "stage_id": "post_export_scoring",
                    "done": 74,
                    "total": 177,
                    "unit": "scoring_batch",
                    "outer": {"done": 57, "total": 101, "unit": "pass"},
                    "details": {"pass_name": "reform_validation"},
                },
                clock(0),
            )
        ]
    )
    details = events[-1]["details"]
    assert details.get("outer") == {"done": 57, "total": 101, "unit": "pass"}
    assert (details["done"], details["total"], details["pass_name"]) == (
        74,
        177,
        "reform_validation",
    )


@pytest.mark.parametrize(
    "outer",
    [
        [57, 101],
        {"done": "57", "total": 101},
        {"done": 57},
        {"done": 57, "total": 101, "unit": 3},
        {"done": True, "total": 101},
    ],
)
def test_an_invalid_outer_counter_is_refused(outer):
    request = {
        "command": "progress",
        "stage_id": "score",
        "done": 1,
        "total": 2,
        "outer": outer,
    }
    with pytest.raises((TypeError, ValueError), match="outer"):
        process_request(LifecycleState(), request, clock(0))


@pytest.fixture
def module(tmp_path):
    run = TelemetryRun("span-run", "US", "test", producer_id="span-producer")
    module = TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            "heartbeat_seconds": 30,
            "development_collector_url": "http://127.0.0.1:1",
        },
        ModuleContext(os.getpid()),
    )
    module.initialize()
    module.delivery = Mock()
    yield module
    module.close(time.monotonic())


def test_an_unexpected_exit_ends_every_open_stage(module):
    module.handle_message(stage("build"))
    module.handle_message(transition("score"))
    module.parent_exited()
    recorded = module.spool.batch("span-run", "span-producer")
    assert [
        (
            event["stage_id"],
            event["status"],
            event["details"].get("stage_span", {}).get("closed_by"),
        )
        for event in recorded[-3:]
    ] == [
        ("score", "failed", "run_failed"),
        ("build", "failed", "run_failed"),
        ("failed", "failed", None),
    ]


def emitted_run_end(status, **extra):
    return {
        "command": "emit",
        "event_type": "run",
        "stage_id": {"completed": "complete", "failed": "failed"}.get(
            status, "blocked"
        ),
        "status": status,
        **extra,
    }


@pytest.mark.parametrize(
    ("status", "details", "stage_status", "closed_by"),
    [
        ("completed", {}, "completed", "run_completed"),
        ("failed", {"error_type": "RuntimeError"}, "failed", "run_failed"),
        (
            "blocked",
            {
                "phase": "preflight",
                "blocking_gate_ids": ["fixture-check"],
                "blocking_failure_count": 1,
            },
            "completed",
            "run_blocked",
        ),
    ],
)
def test_ending_the_run_through_emit_ends_every_open_stage(
    status, details, stage_status, closed_by
):
    state, events = apply(
        [
            (stage("build"), clock(0)),
            (transition("score"), clock(1)),
            (emitted_run_end(status, details=details), clock(2)),
        ]
    )
    assert [
        (stage_id, end_status, (span or {}).get("closed_by"))
        for stage_id, end_status, span in stage_events(events)[-2:]
    ] == [("score", stage_status, closed_by), ("build", stage_status, closed_by)]
    assert (events[-1]["event_type"], events[-1]["status"]) == ("run", status)
    assert state.open_stages == ()
    assert state.transition_stage is None
    assert state.finished


def test_a_stage_inside_a_transition_stage_of_the_same_id_ends_first():
    state, events = apply(
        [
            (transition("score"), clock(0)),
            (stage("score"), clock(1)),
            (transition("export"), clock(2)),
        ]
    )
    assert stage_events(events)[2:] == [
        (
            "score",
            "completed",
            {
                "parent_stage_id": "score",
                "occurrence": 2,
                "awake_seconds": 1.0,
                "asleep_seconds": 0.0,
                "closed_by": "enclosing_stage_ended",
            },
        ),
        (
            "score",
            "completed",
            {
                "parent_stage_id": None,
                "occurrence": 1,
                "awake_seconds": 2.0,
                "asleep_seconds": 0.0,
                "closed_by": "next_stage_started",
            },
        ),
        ("export", "started", {"parent_stage_id": None, "occurrence": 1}),
    ]
    assert [open_stage.stage_id for open_stage in state.open_stages] == ["export"]
    assert state.transition_stage == "export"


def test_ending_the_inner_stage_of_the_same_id_keeps_the_transition_stage():
    state, events = apply(
        [
            (transition("score"), clock(0)),
            (stage("score"), clock(1)),
            (stage("score", "completed"), clock(2)),
        ]
    )
    assert (events[-1]["details"].get("stage_span") or {}).get("occurrence") == 2
    assert state.transition_stage == "score"
    assert [open_stage.occurrence for open_stage in state.open_stages] == [1]


def test_an_untracked_transition_stage_ended_by_a_stage_event_ends_once():
    tracked = [(stage(f"stage-{index}"), clock(index)) for index in range(32)]
    state, events = apply(
        [
            *tracked,
            (transition("score"), clock(40)),
            (stage("score", "completed"), clock(41)),
            (transition("export"), clock(42)),
        ]
    )
    assert [(event["stage_id"], event["status"]) for event in events[32:]] == [
        ("score", "started"),
        ("score", "completed"),
        ("export", "started"),
    ]
    assert state.transition_stage == "export"

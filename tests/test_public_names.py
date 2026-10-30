"""The modules that re-export interface names keep their public names.

Both packages ship ``py.typed``, so type checkers of the code that imports
these modules, such as Microcosm's, read each module's ``__all__`` as its
exports. These lists pin them, so a public name is never dropped or added
without a change here.
"""

import importlib

import pytest

PUBLIC_NAMES = {
    "microcosm_emitter.orrery.client": [
        "INVALID_ARGUMENTS",
        "UNAVAILABLE",
        "GraphPublicationClient",
        "GraphTransport",
        "default_spool_path",
        "publication_inventory",
        "publish_graph",
    ],
    "microcosm_emitter.orrery.contracts": [
        "ClaimedGraphPublication",
        "PublicationFile",
        "PublicationFileRole",
        "PublicationInventory",
        "PublicationReceipt",
    ],
    "microcosm_emitter.telemetry.client": [
        "LocalTelemetryEmitter",
        "TelemetryRun",
        "Transport",
    ],
    "microcosm_emitter.telemetry.constants": [
        "DEFAULT_HEARTBEAT_SECONDS",
        "LOCAL_ONLY_REJECTED_EVENTS",
        "QUEUE_WARNING",
        "REJECTED_EVENTS_MESSAGE",
        "SERVICE_START_WARNING",
        "TELEMETRY_MODULE",
    ],
    "microcosm_emitter.telemetry.protocol": [
        "BUILD_BLOCKED_MESSAGE",
        "BUILD_COMPLETED_MESSAGE",
        "BUILD_STARTED_MESSAGE",
        "CALIBRATION_EVENT_KIND",
        "EVENT_TYPE_CALIBRATION",
        "EVENT_TYPE_HEARTBEAT",
        "EVENT_TYPE_PROGRESS",
        "EVENT_TYPE_RUN",
        "EVENT_TYPE_STAGE",
        "MAX_TELEMETRY_CHECK_ID_CHARS",
        "MAX_TELEMETRY_COLLECTION_ITEMS",
        "MAX_TELEMETRY_DETAILS_BYTES",
        "MAX_TELEMETRY_DETAILS_DEPTH",
        "MAX_TELEMETRY_MESSAGE_CHARS",
        "MAX_TELEMETRY_TEXT_CHARS",
        "SEQUENTIAL_STATUS_MAP",
        "STAGE_BLOCKED",
        "STAGE_CALIBRATING",
        "STAGE_COMPLETE",
        "STAGE_CREATED",
        "STAGE_FAILED",
        "STATUS_BLOCKED",
        "STATUS_COMPLETED",
        "STATUS_FAILED",
        "STATUS_PROGRESS",
        "STATUS_STARTED",
        "TELEMETRY_IDENTIFIER_PATTERN",
        "TELEMETRY_SCHEMA_VERSION",
        "UNEXPECTED_PROCESS_EXIT_MESSAGE",
        "TelemetryEventType",
        "TelemetryStatus",
    ],
    "microcosm_emitter.telemetry.requests": [
        "COMMAND_BLOCK",
        "COMMAND_CALIBRATION_PROGRESS",
        "COMMAND_COMPLETE",
        "COMMAND_EMIT",
        "COMMAND_FAIL",
        "COMMAND_PROGRESS",
        "COMMAND_STAGE",
        "COMMAND_TRANSITION_CALIBRATION_PROGRESS",
        "COMMAND_TRANSITION_STAGE",
        "DEFAULT_FAILURE_CLASS",
    ],
    "microcosm_emitter.telemetry.serialization": ["wire_value"],
}


@pytest.mark.parametrize("module_name", sorted(PUBLIC_NAMES))
def test_a_re_exporting_module_lists_exactly_its_public_names(module_name):
    module = importlib.import_module(module_name)

    assert sorted(module.__all__) == sorted(PUBLIC_NAMES[module_name])
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert missing == []

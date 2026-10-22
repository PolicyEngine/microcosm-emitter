"""Typed hand-overs from Microcosm builds to the separately installed emitter.

Importing this package starts no process and opens no socket or file.
"""

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.client import EmitterClient, RequestFailure
from microcosm_emitter_interface.launcher import (
    COMMAND_ENVIRONMENT_VARIABLE,
    DEFAULT_COMMAND,
    EmitterStartupError,
)
from microcosm_emitter_interface.orrery import OrrerySettings
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseFile,
    WithdrawBuild,
)
from microcosm_emitter_interface.spool import (
    default_handover_database_path,
    default_spool_path,
)
from microcosm_emitter_interface.staging import (
    HuggingFaceSettings,
    RunFile,
    StageRunFiles,
)
from microcosm_emitter_interface.submissions import CollectorProvenance
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings

__all__ = [
    "BundleFile",
    "COMMAND_ENVIRONMENT_VARIABLE",
    "CollectorProvenance",
    "DEFAULT_COMMAND",
    "PROTOCOL_VERSION",
    "EmitterClient",
    "EmitterStartupError",
    "HuggingFaceSettings",
    "OrrerySettings",
    "RequestFailure",
    "PromoteBuild",
    "PublishRelease",
    "ReleaseFile",
    "RunFile",
    "StageDatasetBundle",
    "StageRunFiles",
    "TelemetryRun",
    "TelemetrySettings",
    "WithdrawBuild",
    "default_handover_database_path",
    "default_spool_path",
]

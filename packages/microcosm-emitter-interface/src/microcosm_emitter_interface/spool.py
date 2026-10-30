"""Where the emitter keeps its durable queues."""

import os
from pathlib import Path


def default_spool_path() -> Path:
    """Return the path of the emitter's event queue file.

    It is ``microcosm/telemetry/events.sqlite3`` under ``XDG_CACHE_HOME``, or
    under ``~/.cache`` when that variable is unset or blank: the location
    existing emitters use, so queued work survives upgrades.
    """
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"


def default_handover_database_path(spool_path: Path) -> Path:
    """Return the Hugging Face module's hand-over database path, beside ``spool_path``.

    ``events.sqlite3`` gives ``events.handovers.sqlite3``. It is a separate
    file because released emitters, which Microcosm builds pin and which
    share the event queue file, refuse an event queue file whose migration
    revision they don't know.
    """
    return spool_path.with_name(f"{spool_path.stem}.handovers.sqlite3")

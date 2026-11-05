"""Each migration environment runs only on the connection the emitter supplies."""

import pytest
from alembic import command

from microcosm_emitter.core.migrations import (
    HANDOVER_HISTORY,
    SPOOL_HISTORY,
    alembic_config,
)

HISTORIES = pytest.mark.parametrize(
    "history", [SPOOL_HISTORY, HANDOVER_HISTORY], ids=["event-queue", "hand-over"]
)


@HISTORIES
def test_a_migration_run_without_the_emitters_connection_is_refused(history):
    with (
        alembic_config(history=history) as config,
        pytest.raises(RuntimeError, match="require a programmatically supplied"),
    ):
        command.upgrade(config, "head")


@HISTORIES
def test_a_migration_run_given_something_other_than_a_connection_is_refused(history):
    with alembic_config(history=history) as config:
        config.attributes["connection"] = "not a connection"
        with pytest.raises(TypeError, match="require a SQLAlchemy Connection"):
            command.upgrade(config, "head")

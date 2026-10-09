"""Create the telemetry spool schema.

Revision ID: 20261007_01
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS_TABLE = "telemetry_runs"
_EVENTS_TABLE = "telemetry_events"
_EVENTS_INDEX = "telemetry_events_run_sequence"
_PENDING_UPLOAD_STATE = "pending"


def _create_runs_table() -> None:
    op.create_table(
        _RUNS_TABLE,
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("producer_id", sa.Text(), nullable=False),
        sa.Column("registration_json", sa.Text(), nullable=False),
        sa.Column(
            "next_sequence",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "upload_state",
            sa.Text(),
            nullable=False,
            server_default=_PENDING_UPLOAD_STATE,
        ),
        sa.Column("local_only_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "producer_id"),
    )


def _create_events_table() -> None:
    op.create_table(
        _EVENTS_TABLE,
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("producer_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "producer_id"],
            [f"{_RUNS_TABLE}.run_id", f"{_RUNS_TABLE}.producer_id"],
            name="fk_telemetry_events_run",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="uq_telemetry_events_run_producer_sequence",
        ),
    )


def upgrade() -> None:
    """Create a new database; existing schemas must already be versioned."""
    _create_runs_table()
    _create_events_table()
    op.create_index(_EVENTS_INDEX, _EVENTS_TABLE, ["run_id", "producer_id", "sequence"])


def downgrade() -> None:
    """Remove the local spool schema."""

    op.drop_index(_EVENTS_INDEX, table_name=_EVENTS_TABLE)
    op.drop_table(_EVENTS_TABLE)
    op.drop_table(_RUNS_TABLE)

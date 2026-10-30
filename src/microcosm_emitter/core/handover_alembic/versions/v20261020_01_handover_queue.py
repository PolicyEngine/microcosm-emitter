"""Create the hand-over database's tables.

This history is separate from the spool database's, and its revision IDs
cannot be mistaken for the spool's: each history refuses the other's file.
"""

import sqlalchemy as sa
from alembic import op

revision = "handovers_20261020_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the hand-over database's five tables."""
    op.create_table(
        "staged_run_files",
        sa.Column("repository", sa.Text(), primary_key=True),
        sa.Column("path_in_repo", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("committed_sha256", sa.Text(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "staged_run_deliveries",
        sa.Column("repository", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("files_attempted", sa.Integer(), nullable=False),
        sa.Column("files_committed", sa.Integer(), nullable=False),
        sa.Column("commit_attempts", sa.Integer(), nullable=False),
        sa.Column("commits", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_commit", sa.Text(), nullable=True),
        sa.Column("run_index_entry", sa.Text(), nullable=True),
        sa.Column("run_index_pending", sa.Boolean(), nullable=False),
        sa.Column("run_index_due_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "repository_commit_schedules",
        sa.Column("repository", sa.Text(), primary_key=True),
        sa.Column("last_commit_at", sa.Float(), nullable=False),
        sa.Column("retry_after", sa.Float(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
    )
    op.create_table(
        "handover_jobs",
        sa.Column("handover_id", sa.Text(), primary_key=True),
        sa.Column("destination", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("receipt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "handover_files",
        sa.Column("handover_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    """Drop the hand-over database's five tables, in the reverse order of creation."""
    op.drop_table("handover_files")
    op.drop_table("handover_jobs")
    op.drop_table("repository_commit_schedules")
    op.drop_table("staged_run_deliveries")
    op.drop_table("staged_run_files")

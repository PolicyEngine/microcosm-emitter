"""Add durable graph publication jobs without changing event retention."""

import sqlalchemy as sa
from alembic import op

revision = "20261008_02"
down_revision = "20261007_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_publication_jobs",
        sa.Column("publication_id", sa.Text(), primary_key=True),
        sa.Column("inventory", sa.Text(), nullable=False),
        sa.Column("directory", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("receipt", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("graph_publication_jobs")

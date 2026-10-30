"""Alembic environment for the Hugging Face module's hand-over database."""

from alembic import context
from sqlalchemy.engine import Connection

from microcosm_emitter.core.handover_models import HandoverModel

_MISSING_CONNECTION_ERROR = (
    "hand-over database migrations require a programmatically supplied connection"
)


def run_migrations() -> None:
    """Run migrations on the connection supplied by the emitter service."""

    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        # Alembic ran without the emitter's connection: a missing setting.
        raise RuntimeError(_MISSING_CONNECTION_ERROR)  # noqa: TRY004
    context.configure(
        connection=connection,
        target_metadata=HandoverModel.metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()

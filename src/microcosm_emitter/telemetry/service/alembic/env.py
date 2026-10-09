"""Alembic environment for the local telemetry spool."""

from alembic import context
from sqlalchemy.engine import Connection

from microcosm_emitter.telemetry.service.models import SpoolModel

_MISSING_CONNECTION_ERROR = (
    "telemetry spool migrations require a programmatically supplied connection"
)


def run_migrations() -> None:
    """Run migrations on the connection supplied by the emitter service."""

    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError(_MISSING_CONNECTION_ERROR)
    context.configure(
        connection=connection,
        target_metadata=SpoolModel.metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()

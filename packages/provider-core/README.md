# Microcosm provider core

Shared short-lived collector sessions and one SQLAlchemy/Alembic database history
for independently scheduled telemetry and graph publication. The generic process
runtime does not depend on this distribution. Telemetry and graph clients can
import without loading authentication or persistence; domain service modules use
these implementations in the child process. The graph enqueue API lazily imports
the durable local queue to preserve jobs even when the child is unavailable.

Existing versioned Microcosm spools upgrade without changing event or publication
identities. Unversioned queues are rejected rather than automatically adopted.

# PolicyEngine telemetry

The telemetry client and local telemetry emitter service module for instrumented dataset builds.

The client starts the separately distributed `policyengine-local-service` host and sends sanitized events over its private Unix socket. The service module persists events using SQLAlchemy ORM and Alembic, samples process resources, exchanges existing Hugging Face credentials, and retries delivery to the PolicyEngine collector.

Missing or rejected credentials keep events local and permanently exclude that producer from upload. Collector outages do not interrupt dataset builds. The production destination is package-owned; the explicit development override accepts loopback addresses only.

The `policyengine_telemetry.client` module does not import the service implementation, database, or authentication libraries. Microcosm supplies build identity, metadata, queue location, and stage events through its adapter.

# Microcosm provider telemetry

The thin build adapter and local telemetry emitter service module for instrumented dataset builds.

The adapter starts a separate process using the `microcosm-provider-client` host and sends requests over its private Unix socket. It does not track stages or construct or sanitize events. The telemetry service module interprets requests, tracks the current stage, creates and redacts events, persists them using SQLAlchemy ORM and Alembic, samples process resources, exchanges existing Hugging Face credentials, and retries delivery to the PolicyEngine collector.

Each request's events commit together before acknowledgement. A failed database write leaves both the queue and the service's stage state unchanged. The hosted collector's HTTP event format and the existing queue schema remain unchanged.

Missing or rejected credentials keep events local and permanently exclude that producer from upload. Collector outages do not interrupt dataset builds. The production destination is package-owned; the explicit development override accepts loopback addresses only.

The `microcosm_provider_telemetry.client` module does not import the service implementation, database, or authentication libraries. Microcosm supplies build identity, metadata, queue location, and instrumentation requests through this adapter.

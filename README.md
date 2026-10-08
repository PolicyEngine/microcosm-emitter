# PolicyEngine local services

Two Python distributions separate the local process host from telemetry behavior:

- `policyengine-local-service` owns subprocess startup, a private Unix socket, explicit module loading, parent-process monitoring, and shutdown. It has no telemetry, database, or authentication dependencies.
- `policyengine-telemetry` provides the build-facing client and a separate service module. The service module owns resource sampling, SQLAlchemy ORM persistence, Alembic migrations, authentication, and collector delivery.

The build process imports the client, not the service implementation. It supplies run metadata, its existing queue path, and build identity. Each build starts one local telemetry emitter service automatically. Imports alone create no processes, sockets, files, or network requests.

## Delivery and persistence

The service commits each event before acknowledging its local socket message. The client bounds socket calls and reports telemetry failures without failing the build. Network requests run on the service worker, outside the message-handling lock.

The service uses the existing Hugging Face credential from the environment or local login cache. The collector validates PolicyEngine membership before issuing a short-lived write credential. Missing or rejected credentials permanently mark that producer's events as local-only. Transient collector failures retain events for retry. The production destination belongs to the package; development overrides accept loopback addresses only.

The queue retains the current Microcosm Alembic revision, producer sequences, event IDs, and upload eligibility. Unversioned databases are rejected without schema modification. There is no raw SQL, automatic schema adoption, or parallel legacy implementation. The hosted collector and dashboard do not change.

## Development

Use Python 3.13 or 3.14 on Linux or macOS.

```bash
uv sync --all-packages --locked
bash tools/check-quality.sh
uv run --no-sync pytest -m 'not collector'
bash tools/check-artifacts.sh
```

Tests isolate ambient credentials and use synthetic identities. The artifact check builds source archives and wheels, installs the runtime by itself, then installs telemetry and exercises the real service process and migrations.

The collector compatibility job tests installed wheels against a pinned dashboard collector and disposable PostgreSQL. It covers the HTTP exchange, event ingestion, dashboard run documents, rejected identities, and duplicate delivery after a lost acknowledgement. Local execution requires that pinned collector checkout and a loopback test database; `tools/check-collector.sh` verifies these inputs. Never point this test at a deployed database.

## Publication

Both distributions use the same release version. A published GitHub release triggers version and main-branch ancestry checks, all CI checks, and artifact verification. Only then can the protected `pypi` environment approve publishing the exact tested artifacts. The publish job uses PyPI Trusted Publishing; no long-lived publishing token is supplied through CI.

First publication requires two PyPI pending trusted publishers, one for each distribution, attached to this project's `publish.yml` workflow and `pypi` environment. That external setup must be verified before releasing. No release is created or merged automatically by this project.

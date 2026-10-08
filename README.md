# Microcosm local provider

One local service process runs independent telemetry and completed-graph workers.
The workers share authentication and a durable database, but neither requires the
other to be active. Graph publication does not require a telemetry run ID.

## Packages

- `microcosm-provider-client`: subprocess lifecycle, private Unix socket, explicit
  module selection, parent monitoring and bounded shutdown. No domain, database
  or authentication imports.
- `microcosm-provider-core`: shared collector authentication, SQLAlchemy models
  and Alembic migrations. The collector protocol is unchanged.
- `microcosm-provider-telemetry`: build-facing telemetry client and service module.
- `microcosm-provider-orrery`: explicit graph-file inventory, durable publication
  queue, service module and `microcosm-provider-publish-graph` retry command.
- `@policyengine/microcosm-provider-orrery`: TypeScript graph readers. `/browser`
  fetches and verifies the Orrery document, `/server` reads completed publications
  through a supplied storage adapter, and `/contract` validates public metadata.
  The React component, HTTP endpoints and storage credentials remain in the runs app.

Imports alone create no processes, sockets, files or network requests. Microcosm
supplies build identity and registers both workers using `start_services`. The
generic host creates separate workers and routes messages to the selected module;
one worker's slow HTTP request or failure does not stop the other worker.

## Persistence and delivery

Telemetry commits each event before acknowledging its socket message. Missing or
rejected credentials mark that producer's telemetry as local-only, preserving the
existing policy. In contrast, graph jobs remain eligible for retry after missing,
rejected or temporarily unavailable credentials. They retain the exact inventory,
preserved file paths, upload progress and publication ID across restarts. The graph
client commits a job before waiting for the service; publication is immutable.
Signed storage uploads never receive the collector bearer credential.

The database retains Microcosm's existing revisions and event identities. Alembic
upgrades revision `20261007_01` to `20261008_02` by adding graph jobs. Unversioned
databases are rejected without modification; upgrade them using the existing
Microcosm implementation before migrating. No raw SQL or schema adoption is used.

## Configuration

No new runtime environment variables or secrets are introduced. The service reads
`HF_TOKEN`, then `HUGGINGFACE_TOKEN`, then the existing Hugging Face login cache.
It exchanges that credential with the existing collector; both workers share the
short-lived result. The tracked production destinations are in core constants and
the Orrery queue module. Collector development overrides accept loopback only.
`XDG_CACHE_HOME` optionally changes the existing cache root; the default database
is `~/.cache/microcosm/telemetry/events.sqlite3`. Tests replace credentials and
destinations with synthetic values and loopback or in-memory transports.

The reader package needs no environment variables. Its server adapter receives
storage access from the runs app, which continues to own Vercel Blob configuration,
publication authorization and routes. This change does not modify the calibration
dashboard or deploy either hosted service.

## Development

Use Python 3.13 or 3.14 on Linux or macOS, and Bun 1.4.2 for TypeScript.

```bash
uv sync --all-packages --locked
bash tools/check-quality.sh
uv run --no-sync pytest -m 'not collector'
bash tools/check-artifacts.sh
bash tools/check-reader.sh
```

Python artifact checks build and validate all four source archives and wheels,
install the generic runtime alone, then exercise installed telemetry and combined
telemetry/publication processes. Reader checks install the built tarball into a
separate directory and verify that the browser entry bundles without Node imports.
All new tests use local synthetic data. The pre-existing collector compatibility
job is retained; it requires a pinned checkout and disposable loopback PostgreSQL.

## Release and deferred adoption

All five packages use version `0.1.0`. A published GitHub release verifies tag
versions and main-branch ancestry, runs CI, then publishes exactly the tested
Python artifacts through the protected `pypi` environment. Only after that succeeds
does the reader job publish the tested npm tarball using OIDC and the same approval
environment. No long-lived registry token is committed or required by the workflow.

The GitHub `pypi` environment and its approval/tag policy have been verified. Four
PyPI trusted-publisher bindings and the scoped npm package's first-publication and
trusted-publisher setup require registry-owner confirmation before release; those
external bindings have not been verified. The workflow cannot create them. Both
registries must bind `PolicyEngine/microcosm-local-provider`, `publish.yml` and
environment `pypi`. No release or consumer activation is authorized by this PR.

The draft Microcosm and runs-app consumer PRs can pin this branch's exact Git commit
to install and test before publication. The root private npm manifest exists only
for that source-based test path. Before merging either consumer, publish these
packages, replace Git overrides with registry versions, regenerate lockfiles and
rerun its tests. Neither draft deploys, merges or activates automatically.

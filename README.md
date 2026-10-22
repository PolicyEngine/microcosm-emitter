# Microcosm emitter

This repository publishes two Python distributions with one shared version:

- `microcosm-emitter-interface` (`packages/microcosm-emitter-interface/`) is the small, dependency-free package a Microcosm build imports. It starts the separately installed emitter as the build's child process and offers one typed call per kind of hand-over. See its README for the compatibility rules and protocol fixtures.
- `microcosm-emitter` is the emitter itself: the process host, required telemetry library and completed-graph publication library, installed on its own with `uv tool install microcosm-emitter`. It depends on the interface release of the same version.

The `microcosm-emitter` distribution contains:

- `microcosm_emitter.host` owns subprocess startup, a private Unix socket, explicit module loading, parent-process monitoring, and shutdown. Its code does not import telemetry, database, authentication, or Microcosm modules.
- `microcosm_emitter.telemetry.client` is the thin build adapter. It starts the local telemetry emitter service and sends build requests, without importing the service implementation, database, or authentication libraries.
- `microcosm_emitter.telemetry.service` interprets those requests in the separate process. It owns stage tracking, event construction, credential redaction, resource sampling, SQLAlchemy ORM persistence, Alembic migrations, authentication, and collector delivery.
- `microcosm_emitter.orrery.client` sends graph inventory, enqueue, receipt and retry requests over the same private socket. It does not hash files, open a database, authenticate or upload.
- `microcosm_emitter.orrery.service` validates and hashes the selected graph files, commits publication jobs, and uploads exact bytes with retry and expiring worker reservations. A graph publication requires no telemetry run registration.
- `microcosm_emitter.core` provides service-side authentication and SQLAlchemy/Alembic persistence shared by the telemetry and graph libraries. The generic host does not import it.

Installing `microcosm-emitter` always installs the telemetry library and all its dependencies. Telemetry is not a standalone distribution or an optional extra. The package keeps import boundaries between the host, adapter, and service implementation; they do not need separate installation or publication.

The build process imports only the adapter, not the service implementation. It supplies run metadata, its existing queue path, and build identity. The adapter starts a separate local telemetry emitter service process automatically and sends requests over a private Unix socket. The adapter does not track stages, construct events, or redact telemetry. Imports alone create no processes, sockets, files, or network requests.

## Installing the emitter on its own

`uv tool install microcosm-emitter` installs the `microcosm-emitter` command in its own environment, separately from the build's Python environment. A build then starts `microcosm-emitter host` as its own child process, exactly as the in-process launcher starts `python -m microcosm_emitter.host`:

- the build passes its process ID with `--parent-pid` and the module configuration on standard input, never in process-list arguments;
- the host inherits the build's environment variables and runs as the same user, so it finds the same Hugging Face credential (`HF_TOKEN`, then `HUGGINGFACE_TOKEN`, then the token saved by `hf auth login`);
- the host exits after the build exits. Without `--parent-created-at`, the host reads the build's creation time itself, and refuses to start if the named process is not its parent.

The host is never a separately started system service: such a service would not see the build's environment variables. `--skip-unavailable-modules` starts the host without requested modules that this emitter release does not install, so a newer build can still use an older emitter's modules.

Only a host started by `microcosm-emitter host` answers a `hello` request on the socket. It returns the emitter version, the socket protocol versions it accepts, the modules it loaded and the requested modules it skipped (`microcosm_emitter.compatibility.HelloResponse`). A host started with `python -m microcosm_emitter.host`, as by the in-process launcher, refuses `hello` like any unknown action. `microcosm-emitter version` prints the emitter version and protocol versions as JSON. Every emitter release must keep accepting every protocol version that builds still send (`microcosm_emitter.compatibility`).

With `--skip-unavailable-modules`, the host still refuses to start if none of the requested modules is installed, and refuses each request to a skipped module as it refuses one to an unknown module.

## Delivery and persistence

The service interprets requests such as a stage change or build failure, timestamps and redacts the resulting events, and saves them before acknowledging the socket request. A stage change can complete one stage and start another; both events commit in one SQLAlchemy ORM transaction, and the service changes its stage state only after that transaction succeeds. Periodic retention maintenance runs outside this acknowledgement path.

The adapter bounds socket calls and reports telemetry failures without failing the build. It converts Python arguments into JSON but does not process their telemetry meaning. Network requests run on the service worker, outside the message-handling lock.

`block()` finishes the current stage and records that validation checks refused the build, including the phase, check identifiers, failure count and optional check statuses. `fail(error_code=...)` preserves the caller's failure code alongside the exception type and failure class. Microcosm classifies interruptions, process termination and memory failures; this package forwards those classifications rather than duplicating them. Both outcomes finish telemetry, so later heartbeat or parent-exit handling cannot replace them.

The service uses the existing Hugging Face credential from the environment or local login cache. The collector validates PolicyEngine membership before issuing a short-lived write credential. Missing or rejected credentials permanently mark that producer's events as local-only. Permanent HTTP client errors when registering a run or uploading events also keep only the affected producer's records local; HTTP 408, 429, server errors and network failures remain retryable. HTTP 401 invalidates the short-lived credential for renewal. The production destination belongs to the package; development overrides accept loopback addresses only.

The queue upgrades the existing `20261007_01` Alembic revision to `20261008_02`, adding graph-job records without changing producer sequences, event IDs or upload eligibility. Unversioned databases are rejected without schema modification. There is no raw SQL, automatic schema adoption, or parallel legacy implementation. The hosted telemetry collector and calibration dashboard do not change.

## Graph publication

`GraphPublicationClient.start()` starts the graph module in a separate local emitter service process. `inventory()` asks that process to hash an explicit set of allowed graph files; `enqueue()` returns a pending receipt only after the job commits. `receipt()` reads its result and `retry()` makes a pending job eligible again. Startup or socket failures return an unavailable result, not a claim that a job was saved; the build adapter never writes a fallback database.

Graph requests allow up to 30 seconds for bounded file validation and hashing; telemetry acknowledgements, readiness probes and close requests keep their existing short timeout. Very slow storage can still exceed the graph request limit, so a timeout is not proof that a late service-side commit did or did not occur. Repeating an enqueue with the same publication ID and inventory is idempotent.

The host can also start telemetry and graph modules together with `start_services()`. Each module has an independent worker, so a slow graph upload does not block telemetry requests. Construct the telemetry adapter with `close_service_on_finish=False` when a build must publish graphs after recording completion, failure or refusal. Telemetry still finishes, but explicit outer cleanup closes the shared host after graph requests; standalone adapters close automatically by default. A module-specific graph client does not own shared-host shutdown. Both libraries share an existing HF credential and short-lived collector session, but graph jobs remain pending independently of telemetry event retention or local-only upload eligibility.

Preserve the selected source files until publication succeeds. `microcosm-emitter-publish-graph` asks the service to retry a publication by ID and can re-enqueue a preserved directory on another machine. It never rewrites the original build receipt; the queue and separate latest-status file record retries.

## Hugging Face hand-overs

The `huggingface` module takes over the uploads Microcosm made itself. Microcosm still decides what to hand over and validates its own contracts; the module only writes the bytes it was given, with the build's own Hugging Face credential.

**Where hand-overs are stored.** The module keeps its queue in its own SQLite file, the hand-over database, with its own Alembic history (`core/handover_alembic`). `HuggingFaceSettings` names it: by default it sits next to the spool database (`events.sqlite3` → `events.handovers.sqlite3`), and `handover_database_path` chooses another file, never the spool database itself. The spool database stays at revision `20261008_02`, because every emitter on a machine shares it, including the released emitters (0.2.0 to 0.4.0) that Microcosm builds pin, and those refuse a revision they don't know. Each history also refuses the other's file.

**Repair for a machine that ran an earlier, unreleased build of the hand-over queue.** Before the hand-over database existed, an unreleased build kept four or five hand-over tables in the spool database (`staged_run_files`, `staged_run_deliveries`, `repository_commit_schedules`, `handover_jobs`, and in its later form `handover_files`) and moved the spool database to revision `20261020_03`. No migration history contains that revision now. So on such a machine an emitter started with the `telemetry` or `orrery` module, this one or a released one, refuses the spool database at startup (`Can't locate revision identified by '20261020_03'`), and the build runs without that emitter's telemetry or hand-overs. An emitter started with only the `huggingface` module never opens the spool database and is not affected. Nothing repairs this automatically. To repair it, first stop every Microcosm build and emitter on the machine. The spool database is `$XDG_CACHE_HOME/microcosm/telemetry/events.sqlite3`, or `~/.cache/microcosm/telemetry/events.sqlite3` when `XDG_CACHE_HOME` is unset (or the `spool_path` the build set). Then do one of these:

- **Delete it,** together with `events.sqlite3-journal` beside it if that file exists. This loses everything queued in it and not yet delivered: telemetry events not yet sent to the collector, graph publication jobs not yet published, and the run files and dataset bundles that the earlier build handed over and had not committed. The next emitter creates a new spool database at `20261008_02`.
- **Keep the queued telemetry events and graph publication jobs** by undoing what the earlier build's migration did, as its own downgrade would have: drop its tables and set the revision back to `20261008_02`.

  ```sh
  sqlite3 -bail <spool file> <<'SQL'
  BEGIN;
  -- Stop here, changing nothing, unless the file is at the earlier build's revision.
  CREATE TEMP TABLE repair_guard (revision TEXT NOT NULL CHECK (revision = '20261020_03'));
  INSERT INTO repair_guard VALUES ((SELECT version_num FROM alembic_version));
  DROP TABLE IF EXISTS handover_files;
  DROP TABLE IF EXISTS handover_jobs;
  DROP TABLE IF EXISTS repository_commit_schedules;
  DROP TABLE IF EXISTS staged_run_deliveries;
  DROP TABLE IF EXISTS staged_run_files;
  UPDATE alembic_version SET version_num = '20261008_02' WHERE version_num = '20261020_03';
  COMMIT;
  SQL
  ```

  Unless the file's revision is `20261020_03`, the guard row is refused, `-bail` stops `sqlite3` at that error, and the open transaction is rolled back. So the command changes nothing in a spool database at any other revision, nor in the hand-over database (`events.handovers.sqlite3`), whose tables have the same names. No spool revision uses these table names. The dropped tables hold only the earlier build's undelivered hand-overs, which are lost either way.

**Staging run files** (Microcosm's `staging.py` and `staging_v2.py` uploads). A build hands over its current run files; the module copies their bytes into the hand-over database before answering, so the build may rewrite them at once.

- Files land at `<prefix>/<run_id>/<relative path>` (by default `runs/<run_id>/...`), exactly as the build's writer lays them out. Staging contract version 1 also writes `latest_staging.json` at the repository root, and its `runs.json` index: at commit time the module merges the run's summary into the repository's copy at the head it commits on, keeping other runs, with Microcosm's ordering and JSON formatting. It starts a new index only when the Hub answers that the repository has no `runs.json`; any other download error (a connection error, a timeout, a 429 or 5xx response) fails the commit and retries it after the backoff. Microcosm started from an empty index on any download error, which could drop every other run.
- Each path keeps only the newest bytes, and bytes equal to those last committed are not written again.
- **One commit per repository per cycle.** Each module tick (once a second, on the host's worker thread) commits, for each repository with run files due, every pending path in one commit. An unforced hand-over is due one interval (Microcosm's upload interval) after this machine's last commit to the repository; a forced one (final states, artifacts) at the next cycle. Either way, two commits from this machine to one repository, of run files or bundles, are at least 10 seconds apart; only the last commit at shutdown ignores that spacing, and commits from other machines are not counted. Microcosm's writers made one commit per file, up to eight per upload cycle, and each commit triggered the dataset repository's webhook.
- Uploads stay best-effort. Requests answer within seconds or report that they were not accepted; a failed commit keeps the files queued and retries with backoff (up to five minutes); without a credential the files stay queued and one warning is printed. On shutdown the module commits every repository's pending run files once more, on the host's thread and before it waits for any bundle upload. The one exception is a repository a bundle upload holds at that moment: its run files are committed after that upload, if the upload finishes within the shutdown period (15 seconds); otherwise they stay queued for the next emitter. Nothing raises into the build.
- A commit that merges `runs.json` is pinned to the head it read (`parent_commit`); if another build commits first, the module reads the index again and merges again.
- `read_back_run_files` commits pending files, then compares the Hub's copy of each committed file with the committed bytes. A run with no file this emitter committed (its hand-overs were refused, or another machine's emitter staged it) reads back as `failed` with `NO_COMMITTED_FILES`, never `passed`.

**Staged dataset bundles** (Microcosm's `staging_dataset.stage_bundle`, used by `uk_runtime/rowwise_staging.py` and the Orrery evidence bundle). A build verifies its bundle and writes the sidecars as before, then hands the files over by path, each with its role (`dataset`, `calibration_diagnostics`, `build_manifest`, `staged_manifest`, `checksums`, ...), SHA-256 and size. The hand-over is a durable job:

- Before answering, the module copies the bundle's small files into the hand-over database, smallest first, each of at most 32 MiB and together at most 64 MiB, after checking each one's size and SHA-256 against the hand-over (`FILE_SIZE_DIFFERS`, `FILE_DIGEST_DIFFERS`). The receipt's `copied_files` names them, and the build may rewrite them at once: Microcosm rewrites its build manifest, its `sha256sums.txt` and, in national builds, its build record right after staging, and the commit carries the bytes as they were handed over. The copies are deleted once the hand-over settles.
- Larger files (the datasets) are uploaded from their paths. The worker re-hashes each one first and never uploads changed bytes (`PRESERVED_BYTES_CHANGED`); a missing file waits for an operator to restore it (`PRESERVED_FILES_MISSING`).
- As in Microcosm, it writes every file under `<prefix>/<run_id>/` (by default `staged/<run_id>/`) in one commit pinned to the head it checked, with the message `Stage <run_id> under <prefix>/<run_id>`. A repository copy of the bundle's `staged_manifest` whose `files` equal the outputs' digests is `already_staged` with that copy's commit; a different one is `REMOTE_DIFFERS` and is never overwritten.
- Unlike Microcosm's single attempt, `REPOSITORY_UNAVAILABLE` and `UPLOAD_FAILED` are retried with backoff, and a commit that loses a race to another writer is checked and tried again. The receipt keeps the last error code while the job is pending.
- `check_write_access` is Microcosm's staged-dataset preflight: whether a credential is present, the repository is reachable, and the Hub reports a write scope for it.

**Bundle uploads run on their own thread.** A bundle upload can take minutes, so the module uploads bundles on a worker thread of its own, while the host's worker thread commits run files and makes the commit at shutdown. A bundle upload to one repository therefore never delays run-file commits to another repository, the commit at shutdown, or a read-back. Once shutdown begins, the bundle worker starts no further upload, even to a repository left in its current pass: it checks for shutdown before each repository, again once it has leased the repository, and again once it has claimed a job, immediately before that upload begins, and it gives back a job claimed after shutdown began without attempting it. So during the commit at shutdown a repository can be held only by an upload that passed that last check before shutdown began, or, for the few database operations it takes to give it back, by a lease the worker took just before; the second commit at shutdown covers that repository if the worker stops within the shutdown period. Each delivery leases its repository first, so this machine never commits to one repository twice at once: run files for the repository a bundle is uploading to wait for that upload, and then for the 10-second spacing.

**Releases.** The release writer replaces the commit, tag and pointer-file writes of Microcosm's `release.py`. Nothing calls it yet; Microcosm switches to it in a later change. Each build is published side by side and never changed:

```
releases.json                                  the only record of what is current
releases/<release>/<build>/release_manifest.json
releases/<release>/<build>/...                 the build's files, with their roles
```

- `publish_release` names the release, the build (Microcosm's attempt ID), whether to make the build current, the release's role, and each file with its role. `promote_build` changes a release's current build; `withdraw_build` marks a build withdrawn, and its folder stays.
- Every change is one commit: the writer reads `releases.json` and the file list at `main`'s head, applies the change, runs the shared checks in `microcosm_emitter_interface.releases`, and commits the new build folder and `releases.json` with `parent_commit` set to that head. If another commit lands first, it reads the new head and applies the change again, so concurrent publications keep each other's builds.
- The writer refuses a change unless: every listed build has its folder and a `release_manifest.json` naming the same release and build; `current` names a listed build that is not withdrawn; existing build entries change only `withdrawn`; and no file inside an existing build folder is added, changed or removed (so publishing refuses a build whose folder exists). It never writes a `latest*.json` file, and refuses to change `current` while any `latest*.json` file still exists, until the old pointer files are deleted.
- It creates no Hugging Face tags or branches, no `current/` folder and no mutable root file, and leaves existing tags and files untouched. It does not port `release.py`'s staging branch, release tag or pointer-file commit, which this layout replaces.
- Each request carries a `request_id`, a new random one unless the caller gives one, and the emitter applies each request once. Sending the same request again returns its receipt; a new request is applied even if it repeats an earlier change (promoting a build back after another was promoted, or retrying a withdrawal or a refused change once its cause is gone).
- A publication of a build already listed with a byte-identical `release_manifest.json` reports `published` with the commit that wrote it, as when a retried attempt finds that its earlier commit landed, even if another build has been made current since. It never makes that build current; `promote_build` does. Hub failures stay `pending` and are retried; broken rules are `refused` with the rule's code.
- Every release receipt has a `current` field. On a `published` receipt it is the release's current build in `releases.json` once the change was applied (or, when nothing needed writing, as the writer found it), and `null` when the release has none; on every other status it is `null`. So a publication with `make_current` made its build current exactly when its receipt is `published` and `current` equals its build.
- **A queued change never undoes a newer change to the same release, and never lands much later.** A change still `pending` an hour after its hand-over (a Hub outage, a missing credential, a token that cannot write) becomes `refused` with `REQUEST_EXPIRED`, so it cannot land days later, for example after `hf auth login`. Order is kept per release: changes to different releases of one repository never affect each other. Only changes that set `current` are ordered: a promotion is `refused` with `NEWER_CHANGE_LANDED` when a promotion or a publication that makes its build current, to the same release and requested after it, has already landed. A publication that makes its build current is still `published` in that case: it adds its build and leaves `current` as the newer change set it; the receipt's `current` names that build, and its `message` says why. So two publications that land at once both keep their builds, and the newer request's build is current. The writer knows this machine's own changes that landed, and every writer commit's title ends `[newest requests: <release>=<time>; ...]`, each release's newest request time among the changes on the record; a release's time leaves the titles once it is two hours older than the Hub's date for the last commit to `releases.json`, when every change requested before it has expired. A withdrawal never changes `current` (the current build cannot be withdrawn, and a withdrawn build cannot become current, whichever change lands first), so, like a publication that only adds a build, it is never refused for order and does not count as newer. One case remains for the expiry alone: another machine's newer change that left `releases.json` unchanged (promoting the build that is already current) makes no commit, so it records no time, and an older change to that release can still land within the hour. Request times come from each machine's clock, so when two machines' clocks differ, changes requested closer together than that difference can still land out of order. Every change is requested before its commit, so a time in a title more than five minutes after the Hub's date for that commit can only come from a clock that runs fast: it is ignored, and that machine's change is ordered only by its own queue and the expiry. One fast machine therefore neither blocks later changes to its release on other machines nor pushes other releases' times out of the titles.
- A release change goes at the next cycle, even within 10 seconds of this machine's last commit to the repository; bundles and run files keep that spacing. Each cycle still makes at most one commit per repository.
- `release_change_policy` is where a decision on who may promote or withdraw will plug in. It receives the validated change and its caller (`ReleaseCaller`: the user the emitter runs as and the build process that started it) when the change is handed over; a refusal is answered at once as `NOT_AUTHORIZED` and never queued. By default every change is allowed. `current` may stay `null`, for a release that is never promoted.

## Collector submissions

The collector's artifact intake (PolicyEngine/microcosm-collector, `docs/artifact-submissions.md`) takes the files its workers build from: calibration diagnostics for the calibration trees, and the Orrery inputs it projects. The `huggingface` module sends them there from staged dataset bundles, so Microcosm needs no separate call.

- **Only described bundles.** A bundle hand-over may carry `CollectorProvenance`: the country, the build (Microcosm's attempt ID), whether the build used licensed microdata, and the release (`None` for a staged build). Without it, the bundle goes only to Hugging Face, as before.
- **Disclosure** (decision D10). Only the Orrery outputs of a build that used licensed microdata stay private until output clearance. Calibration data may be public, licensed or not, and native evidence is never published.
- **What is submitted.** A `calibration_diagnostics` file gives a `calibration-diagnostics` submission, with any `build_manifest`, `release_manifest` and `demographics` file. `orrery_schema` and `execution_overlay` together give an `orrery-inputs` submission, with any `orrery_upstream` and `orrery_evidence` files, under the collector's role names (`schema`, `execution_overlay`, `upstream`, `evidence`). Dataset files and sidecars are not submitted.
- **After the Hugging Face commit.** The submission's provenance names the repo, the commit that added the bundle's folder (never a later head) and the folder, so the collector row of the hand-over waits for the Hugging Face row. A bundle Hugging Face refuses is `skipped`, and so is one whose folder is longer than the collector's 512 characters (`COLLECTOR_FOLDER_TOO_LONG`).
- **The steps.** The module exchanges the build's Hugging Face token for an `artifacts:submit` credential, creates the submission, uploads each file the collector still lacks to its signed URL with exactly the instruction's headers (only `https://storage.googleapis.com`, or a loopback address in development, and only with a `Host` that names the URL's host), and completes it. Files copied at hand-over are submitted as they were then; the others are re-hashed first.
- **Never in the way of Hugging Face.** Submissions run on a worker thread of their own, so a slow or stalled submission never delays a Hugging Face commit, either commit at shutdown, or a read-back. At shutdown the collector worker starts no new submission; the module makes both Hugging Face shutdown commits first, then waits at most 2 seconds, within the shutdown period, for a submission in progress. A submission still running then is abandoned with the process. While an attempt runs, its hand-over's lease (two minutes) is renewed every 30 seconds, so another emitter on the same machine, which shares the hand-over database, never starts the same submission alongside it, however long the upload takes. Renewal stops when the attempt ends or shutdown begins, so after an abandoned attempt the next emitter on the same machine retries the submission within two minutes (on Modal the hand-over database goes with the container). Each upload waits at most 60 seconds to connect and for the answer. The file is sent in 64 KiB pieces, and each piece must be written to the connection within 60 seconds: over HTTPS, which every Cloud Storage upload uses, a piece is written whole under one time limit, so the link must average at least 1,093 bytes a second (about 8.7 kbit/s) over each piece; over plain HTTP (a loopback development collector) a send fails only after 60 seconds in which no byte was accepted. A stalled upload therefore fails within about a minute and is retried. An upload at or above that rate is never stopped, however large the file: a signed upload stores nothing unless it completes, so stopping it would only start it again from zero. Completion asks for the collector token again, so uploads that outlast the token (an hour) still complete in the same attempt. A cycle starts no new submission after ten minutes, but never stops one in progress.
- **Answers** (collector#6's contract at `673df3d`):
  - The module uses a token only when the exchange answers with `"scope": "artifacts:submit"`. Any other 200, a 404 from the exchange, or a 404 from a submission path without `submission_not_found` means the collector has no intake: the hand-over stays `pending` (`COLLECTOR_INTAKE_NOT_ENABLED`), the token is discarded, and nothing more is sent to that collector for an hour.
  - A 404 `submission_not_found` at completion means the collector lost the submission: the next attempt creates it again.
  - A 403 without a `code` means the token's scope does not allow the request: the module exchanges again once, and only a second such refusal is final. A 403 `submission_owned_by_another_user` is final.
  - Completion answering `rejected` or `failed` (for example `stored_object_mismatch`) is `failed`, with the rejection's code. Outages are retried with backoff; other refusals are `failed`; success is `submitted`, with each submission's ID and status. `collector_submission_receipt` reports it. The module does not poll the submission's status or fetch its receipt yet.
- **Off by default.** Nothing is submitted unless the module configuration sets `collector_submissions` to `true`, which no interface release does yet. `development_collector_url` points the module at a loopback collector. Collector settings it cannot use switch submissions off with one warning, and an error queueing a bundle for the collector is printed once; neither stops Hugging Face uploads or changes the bundle's answer. Switching submissions on for production builds is a separate emitter change.

## Development

Use Python 3.13 or 3.14 on Linux or macOS.

```bash
uv sync --locked
bash tools/check-quality.sh
uv run --no-sync pytest -m 'not collector'
bash tools/check-artifacts.sh
bash tools/check-changelog.sh
```

Tests isolate ambient credentials and use synthetic identities. The artifact check builds one source archive and wheel, installs that wheel into a clean environment away from the source checkout, verifies import boundaries, and exercises telemetry-only, graph-only and combined service startup with packaged migrations.

The collector compatibility job tests the installed wheel against a pinned dashboard collector and disposable PostgreSQL. It covers the HTTP exchange, event ingestion, blocked outcomes and supplied failure codes in dashboard run documents, rejected identities, and duplicate delivery after a lost acknowledgement. Local execution requires that pinned collector checkout and a loopback test database; `tools/check-collector.sh` verifies these inputs. Never point this test at a deployed database. The pin verifies source compatibility, not which revision the hosted collector currently runs.

## Publication

Every merge to `main` releases both Python distributions, `microcosm-emitter` and `microcosm-emitter-interface`, to PyPI with one shared version. Each pull request adds a changelog fragment named `changelog.d/<name>.<type>.md`, where the type is `breaking`, `added`, `changed`, `fixed` or `removed`; `tools/check-changelog.sh` refuses a pull request without one.

`push.yaml` runs twice for each merge:

1. On the merge commit, it bumps the version in both `pyproject.toml` files, the emitter's exact `microcosm-emitter-interface` pin and `uv.lock`: a major bump for a breaking fragment, a minor bump for an added or removed fragment, and a patch bump otherwise. It collects the fragments into `CHANGELOG.md` and commits the result as "Update microcosm-emitter" using the PolicyEngine GitHub App, which may bypass the `main` ruleset.
2. On that commit, it runs every CI check, tags the commit with the bare version (for example `0.2.0`), and publishes the tested wheels and source archives of both distributions to PyPI. Each step starts only after the one before succeeds.

The publish job uploads `microcosm-emitter-interface` before `microcosm-emitter`, because the emitter requires the interface of the same version: if the interface upload fails, the emitter is not uploaded. Both uploads skip files PyPI already holds, so rerunning a failed publish job uploads only the missing files.

PyPI accepts uploads only through trusted publishing from `push.yaml` in the `pypi` environment, which only `main` can use; each of the two PyPI projects needs its own trusted-publisher entry for that workflow and environment. CI holds no long-lived registry token. Renaming the workflow or the environment stops publication until PyPI's trusted-publisher settings change to match.

The artifact check (`tools/check-artifacts.sh`) builds both distributions, installs the emitter with its interface into one clean environment, as `uv tool install` would, and the interface alone into another, as a build's environment would. From the second, it starts the first's `microcosm-emitter` command and sends telemetry and an Orrery hand-over.

No hosted-service deployment or consumer activation follows from publishing these packages.

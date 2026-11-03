## [0.12.4] - 2026-11-03

### Changed

- The total number of inline suppressions may not grow: a ratcheted rule lists the repository's 140 suppression comments (`# noqa` and Ruff's own forms) in `rules/baselines/noqa-comments.txt`, and a pull request may add one only by removing another. The check counts comments only, so a swap at the same total, a rename or a code added to an existing comment passes. The wheels are unchanged.


## [0.12.3] - 2026-11-03

### Changed

- Complex functions are split into helpers and magic values in comparisons are named constants. Fifteen definitions keep a `# noqa` for too many arguments, with its reason: eleven signatures whose keyword arguments callers pass by name or that the standard library fixes, and four pytest test functions. Ruff no longer ignores `C90` and `PLR`, so it now ignores only `COM812`, `ISC001`, `D203`, `D213` and `CPY`, plus the registered per-file ignores. Behaviour is unchanged.


## [0.12.2] - 2026-11-03

### Changed

- The smaller Ruff rules apply: implicit string concatenations in collections are parenthesized, tests assert one condition at a time and match the messages they expect, and returns, comprehensions, paths and suppressions are written as Ruff requires. Fifteen findings keep a `# noqa` with its reason: unused arguments whose signatures their callers fix (ARG) and two overrides of `log_message(format)` (A002). Ruff no longer ignores `ISC`, `PT`, `RET`, `ARG`, `PTH`, `E`, `SIM`, `RUF`, `FBT`, `C4`, `PERF`, `PLW` and `A`. Behaviour is unchanged.


## [0.12.1] - 2026-11-03

### Changed

- Broad excepts, prints and the other kept security findings carry a `# noqa` with their reason. All 21 asserts in package code become explicit checks that raise the same `AssertionError`, so they also hold under `python -O`. The two modules that run as programs may print, and Ruff no longer ignores `BLE`, `S`, `T20` and `SLF`.


## [0.12.0] - 2026-11-03

### Added

- The interface's `serialization` module exports `JsonValue`, the type of a JSON value (null, a boolean, a number, a string, an array or an object), which `wire_value` returns.

### Changed

- Imports used only in annotations move under `TYPE_CHECKING`, modules that re-export interface names list their public names in `__all__`, every function is annotated (the interface's `serialization` module gains a `JsonValue` type alias for JSON values), the root `conftest.py` moves to `tests/conftest.py`, and Ruff no longer ignores `TC`, `ANN` and `PLC`. Behaviour is unchanged.


## [0.11.11] - 2026-11-02

### Changed

- Docstrings follow Ruff's `D` rules: every public module, class, function and method has one, every summary fits on the first line, and function and method summaries are in the imperative mood. Ruff no longer ignores the family. Behaviour is unchanged.


## [0.11.10] - 2026-11-02

### Changed

- Exceptions are raised as Ruff's `EM`, `RSE` and `TRY` rules require: messages are assigned before the raise, checks that raise inside a `try` block are moved into helper functions, and Ruff no longer ignores these families. Behaviour is unchanged.


## [0.11.9] - 2026-11-02

### Changed

- Ruff now selects every rule (`ALL`), as the simulation API and the Public API do, with their four global ignores (COM812, ISC001, D203, D213) and their per-file ignores for tests and command-line scripts, each registered as a rule, and with the copyright-notice rules (CPY) off. The rule families with findings not fixed yet are ignored until the pull requests stacked on this one fix them. The wheels are unchanged.


## [0.11.8] - 2026-11-02

### Fixed

- Dependabot's pull requests that change only `uv.lock` now pass "Check changelog fragment", and a merge without changelog fragments now ends the Push workflow successfully instead of failing it: the changelog job and the versioning job, which can finish without running uv, no longer use setup-uv's cache, whose save step failed when uv had created no cache directory. A registered rule checks every workflow for such a job. The README, the Push workflow's comments and four rule statements now say exactly what the code and the checks do. The wheels are unchanged.


## [0.11.7] - 2026-11-02

### Changed

- A new "PR description" check, in `pr-description.yaml`, fails a pull request into `main` unless the first line of its description is `Fixes #N` and `N` is an open issue in this repository. It is registered as `emitter.process.pr-tracking-issue`. The wheels are unchanged.


## [0.11.6] - 2026-10-30

### Changed

- Pull requests now refuse a workflow `run:` step that holds more than one command, a script under `tools/` that a workflow calls but no test names, and a script under `tools/` that shellcheck faults: the rules registry's ci-scripts check runs in the "Rules registry" job, and the registry guard refuses a ci-scripts baseline that lists more than on the base branch. Every such script now has tests that run it with fake commands. The wheels are unchanged.


## [0.11.5] - 2026-10-30

### Changed

- CI installs uv 0.13.6 in every workflow, read from `.github/uv-version.txt`, and the project's `[tool.uv] required-version` refuses older uv releases. CI runs `uv lock --check`, and runs every command in the environment `uv.lock` locks (`uv run --locked`), except registered sites that cannot, such as the collector contract test against the built wheels and the installed-artifact check, which installs the wheels as a user would. Dependabot now proposes `uv.lock` updates weekly as one pull request; such a lock-only pull request needs no changelog fragment, and its merge releases nothing. Registered rules check all of these. The wheels are unchanged.


## [0.11.4] - 2026-10-30

### Changed

- Pull requests now refuse a Python file longer than 800 lines, or 1,500 for test code (files under `tests/` and the rules tool's own tests): the rules registry's file-length check runs in the "Rules registry" job. The Hugging Face hand-over queue's job records moved from `huggingface/service/queue.py` to `huggingface/service/queue_jobs.py`, which brings `queue.py` under the limit; every name it offered is still importable from it, and nothing else changes.


## [0.11.3] - 2026-10-30

### Changed

- The rules registry now lists every rule of the repository: each check pull requests already run (Ruff, `ruff format`, `bash -n` on `tools/*.sh`, actionlint, the changelog check, the tests, the package and installed-artifact checks, the collector contract test, and the structure tests for import boundaries, handwritten SQL, workflows, distributions and protocol fixtures), and the eight rules once written by hand in `AGENTS.md`. `AGENTS.md`'s Rules section and `docs/generated/rules.md` are generated from it, and the registry guard reads both `pr.yaml` and `ci.yml`, so a job in either workflow, or a test in one of the structure-test files the registry names, that no rule describes fails it. The wheels are unchanged.


## [0.11.2] - 2026-10-30

### Changed

- Pull requests now check the repository's rules registry. `rules/registry.toml` lists the repository's registered rules; the "Rules registry" job runs the rules tool's tests and `python -m scripts.rules guard`, which fails when a rule names a check that does not exist or a CI job that does not run it, when CI runs a check no rule describes, or when a rule or its list of tolerated violations is weaker than on `main`. `python -m scripts.rules render` writes `docs/generated/rules.md` and the Rules section of `AGENTS.md`. The tool is a copy of the simulation API's, reading what differs between repositories from the registry's `[repository]` table. The wheels are unchanged, the build leaves the tool, the registry and their documents out of the `microcosm-emitter` source archive, and the artifact check refuses an archive that holds them.


## [0.11.1] - 2026-10-29

### Fixed

- The telemetry module no longer stops in two cases. A login saved by `hf auth login` that this user may not read, or that is not text, with neither `HF_TOKEN` nor `HUGGINGFACE_TOKEN` set, keeps runs pending with the reason `huggingface_login_unreadable`, with one warning that names only the error's type; the login is read again with backoff, and once it can be read the runs are delivered. As soon as the login can be read again, the reason is cleared, including for runs already registered, and the shutdown drain retries a collector outage as usual. Other errors from huggingface_hub, such as a failed OIDC token exchange while `HF_OIDC_RESOURCE` is set, are not reported as an unreadable login: the telemetry module handles them as before, and `microcosm-emitter telemetry-token` now exits 7 (an unexpected error, naming its type) instead of 3, because the exchange fails before the collector is contacted. A collector answer that is not valid HTTP or ends early, such as a truncated chunked answer, during run registration or event upload is retried with backoff like other network failures, and the run's state does not change. The token exchange also refuses a collector `access_token` with characters other than visible ASCII, such as a space, as an answer without a usable token, so `microcosm-emitter telemetry-token` exits 5 instead of printing a token that the emitter given it would refuse, and a workstation's telemetry module retries the exchange instead of stopping.


## [0.11.0] - 2026-10-29

### Added

- `microcosm-emitter telemetry-token --lifetime-seconds N` exchanges this machine's Hugging Face credential for a telemetry-only collector token and prints it, its expiry and the collector origin as JSON on standard output, with its own exit code for each failure; it never prints the Hugging Face credential, nor the token anywhere else. An emitter given such a token in `MICROCOSM_COLLECTOR_TOKEN`, `MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT` and `MICROCOSM_COLLECTOR_ORIGIN` uses it instead of a Hugging Face credential and sends it only to that collector, so the Orrery module's jobs wait with `credential_withheld_from_destination`. The origin variable is only checked, and an unusable value in any of the three stops an emitter with the telemetry or Orrery module at start, before it accepts any request from the build; one with only the Hugging Face module starts, with collector submissions off. From 30 seconds before that token expires, or after a 401, runs stay pending for a later emitter with a Hugging Face login instead of becoming local-only. A Hugging Face credential with characters other than visible ASCII, such as a pasted space or a line break, cannot be sent as a bearer token: it is never sent, counts as refused, gets its own telemetry warning, and makes the Orrery module's jobs wait with `credential_unsendable`. `microcosm-emitter version` also prints the collector origin.


## [0.10.0] - 2026-10-28

### Added

- - Send to the collector's production hostname by default (`PRODUCTION_COLLECTOR_URL`), instead of the Cloud Run address of the same production service. The hostname stays the same when the collector moves to another Google Cloud project. The Cloud Run address stays in `TRUSTED_COLLECTOR_ORIGINS` as `CLOUD_RUN_COLLECTOR_URL`, so a module configuration's `collector_origin` can still select it. No interface release passes that setting today, so rolling back the hostname needs a new emitter release. No environment variable selects the destination.
  - Treat the hostname and the Cloud Run address (`PRODUCTION_COLLECTOR_ORIGIN_PAIR`) as one collector: collector submissions queued for either are delivered through the origin the emitter resolves. Submissions that earlier releases queued for the Cloud Run address are submitted and checked through the hostname after an upgrade, and a bundle handed over again joins its queued submission instead of printing "A bundle could not be queued for the collector". Submissions queued for any other origin, such as a development collector or another trusted origin, go only to an emitter at that origin.


## [0.9.0] - 2026-10-28

### Added

- Choose the collector origin in one place for every service module: telemetry, Orrery graph publication, and the Hugging Face module's collector submissions. A module's new `collector_origin` setting may select any origin in the package's trusted set, `TRUSTED_COLLECTOR_ORIGINS`, and is refused otherwise: the telemetry and Orrery modules fail to start, and the Hugging Face module switches collector submissions off with one warning. The set holds only the current production collector, so the default and every accepted value are unchanged. No environment variable selects the destination, and the development URL still must be on loopback.


## [0.8.0] - 2026-10-27

### Added

- Submit staged bundles' calibration diagnostics and Orrery inputs to the collector's artifact intake, off by default: a bundle hand-over may carry `CollectorProvenance` (country, build, licensed data, release), and with `collector_submissions` switched on the `huggingface` module submits the files it names after the bundle's Hugging Face commit, through the `artifacts:submit` credential, signed uploads and completion, retrying outages from the durable hand-over queue. Once completion accepts a submission, the emitter checks its status in the background (`GET /v1/submissions/{id}`, 10 seconds after acceptance, then twice as long each time up to ten minutes, for at most a day) and, once it is `succeeded`, `rejected` or `failed`, stores the collector's receipt and reason; builds never wait for this, and the checks resume in the next emitter on the machine. The interface's new `EmitterClient.collector_submission_receipt` answers at once with each submission's state, and a rejected, failed or timed-out submission is also reported on standard error. It follows the contract in microcosm-collector's `docs/artifact-submissions.md`: a token is used only when the exchange grants `artifacts:submit`, and a collector without intake leaves the hand-over pending for an hour. Calibration data may be public; only the Orrery outputs of licensed builds stay private until cleared, and native evidence is never published. Submissions run on a worker thread of their own, so they never delay Hugging Face commits or the commits at shutdown; an attempt keeps its hand-over's two-minute lease renewed while it runs, so a second emitter on the same machine never repeats the same upload alongside it; every request has a time limit, and an upload is never stopped for its total time: it fails only when one 64 KiB piece of a file takes more than 60 seconds to send (an average below about 8.7 kbit/s over that piece).


## [0.7.0] - 2026-10-22

### Added

- Add the release writer and the interface's `publish_release`, `promote_build`, `withdraw_build` and `release_receipt` calls. Each build is written to `releases/<release>/<build>/` with an updated `releases.json` in one commit pinned to `main`'s head, re-applied after a parent-commit conflict, and checked against the shared rules: no new tags, no `latest*.json` files, no change to an existing build folder, and no change of `current` while any `latest*.json` file exists. Each request carries a `request_id`, so a repeated change is applied again while a resent request is applied once. Nothing calls the writer yet. A change still pending an hour after its hand-over is refused (`REQUEST_EXPIRED`), and a promotion is refused (`NEWER_CHANGE_LANDED`) when a promotion, or a publication that makes its build current, to the same release and requested after it landed first, so a queued change never puts back an older current build. Withdrawals never change `current`, so they are never refused for order and never count as newer. Request times more than five minutes after the Hub's date for the commit that records them are ignored, so one machine whose clock runs fast cannot block or unprotect other machines' changes. A publication overtaken in that way still lists its build and leaves `current` as the newer change set it, so two publications that land at once both keep their builds. Every release receipt names the release's current build after the change (`current`), so a caller can tell whether a publication made its build current.
- Add `microcosm_emitter_interface.releases`: the shape of each dataset repository's `releases.json`, the changes the release writer makes (publish a build, make a build current, withdraw a build), and the checks every write must pass, so Microcosm and the emitter run the same code. A `releases.json` above 4 MiB, or nested too deeply to decode, is refused as invalid, and so is a manifest nested too deeply.


## [0.6.0] - 2026-10-21

### Added

- Add `stage_dataset_bundle`, `dataset_bundle_receipt` and `check_write_access`. A build hands over its staged dataset bundle with each file's role; the emitter writes it under `staged/<run_id>/` in one commit with Microcosm's message, paths and idempotency (`already_staged`, `REMOTE_DIFFERS`), and retries Hub failures from the durable queue. The emitter copies the bundle's small files (each up to 32 MiB, up to 64 MiB per hand-over) into its queue before answering, so the build may rewrite them at once, as Microcosm does with its build manifest, checksum listing and build record; it uploads larger files from their paths and never uploads one whose bytes changed after the hand-over. Bundles upload on a worker thread of their own, so a long upload never delays run-file commits to another repository, the commit at shutdown, or a read-back.
- Add a durable Hugging Face hand-over queue in its own SQLite database next to the spool database (`events.handovers.sqlite3`, with its own Alembic history), so the shared spool database stays at a revision that released emitters accept; `HuggingFaceSettings.handover_database_path` can name another file. Staging run files keep only the newest handed-over bytes per repository path; dataset bundles and releases are jobs with leases, retries and receipts; and a per-repository schedule spaces commits and lets one writer on a machine hold a repository at a time. Hugging Face calls use the build's own credential, found in the same order as telemetry's.
- Add the `huggingface` emitter module and the interface's `stage_run_files`, `run_files_receipt` and `read_back_run_files` calls. A build hands over its staging run files; the emitter writes them at the same paths with the same bytes as Microcosm's staging writers (`runs/<run_id>/...`, and for staging contract version 1 `latest_staging.json` and a merged `runs.json`), in one Hugging Face commit per repository per cycle instead of one commit per file. Uploads stay best-effort and are retried from the durable queue.


## [0.5.1] - 2026-10-21

### Fixed

- - Lock certifi 2026.10.19, which removes the "emSign ECC Root CA - G3" root. The previously locked certifi 2026.7.22 trusted eMudhra intermediate CA certificates whose private keys can be recovered from their public certificates (https://emudhra.we-have-a-problem.com), so a development or CI environment synced from the lockfile could accept forged certificates for the collector or Orrery publication hosts. Only the lockfile changes: `uv tool install microcosm-emitter` resolves its own dependency versions.


## [0.5.0] - 2026-10-21

### Added

- Add the `microcosm-emitter-interface` distribution: a dependency-free package that a Microcosm build depends on with a version range. `EmitterClient.start` runs the installed `microcosm-emitter` command as the build's child process, which inherits the build's environment and finds the same Hugging Face credential, and refuses an emitter that does not accept its socket protocol version. It offers typed calls for telemetry events and Orrery input hand-overs, and never raises into the build: not because the emitter is missing or refusing a request, and not because an argument cannot be converted to JSON. A refused call reports why in `EmitterClient.last_failure`. The emitter's in-process telemetry and Orrery clients now build their messages with the interface's code, and the in-process Orrery client reports an argument it cannot send (`invalid_arguments`) instead of raising.


## [0.4.0] - 2026-10-20

### Added

- Add the `microcosm-emitter` command, so `uv tool install microcosm-emitter` installs the emitter outside the build's Python environment. A build starts `microcosm-emitter host` as its own child process, which inherits the build's environment and user. The host can read its parent's creation time itself, and can skip requested modules that it does not install (it still refuses to start if none is installed). Only a host started by `microcosm-emitter host` answers `hello`, with the emitter version, accepted socket protocol versions and loaded and skipped modules; a host started with `python -m microcosm_emitter.host` refuses it.


## [0.3.0] - 2026-10-14

### Removed

- Remove the unused TypeScript reader `@policyengine/microcosm-provider-orrery`, so each release now publishes only the `microcosm-emitter` Python distribution to PyPI.


## [0.2.1] - 2026-10-14

### Fixed

- Publish the reader tarball by its relative path. npm treated the bare reader-dist path as a GitHub repository and never published the reader for 0.2.0.


## [0.2.0] - 2026-10-14

### Added

- Extract the local-service host, thin build adapter, and required telemetry library into the `microcosm-emitter` distribution.
- Add independent completed-graph publication and verified Orrery document readers alongside telemetry in the local provider.

### Changed

- Release automatically on every merge to main: bump one shared version from the changelog fragment types, commit the changelog, run all checks, tag the commit with the bare version, then publish to PyPI and npm through trusted publishing. Pull requests now require a typed changelog fragment, and GitHub releases no longer trigger publication.
- Define explicit types for publication inventories and results, and return a frozen record when reserving a graph upload.
- Integrate completed-graph publication into the single microcosm-emitter package. Keep graph adapters limited to socket requests and preserve shared SQLAlchemy/Alembic persistence, telemetry lifecycle behavior and separate TypeScript readers.
- Consolidate the process host and required telemetry library into one `microcosm-emitter` distribution, including Python imports, installed-module registration, CI, artifact verification, and publication.

  Rename the GitHub project to `PolicyEngine/microcosm-emitter` and update source metadata, installation checks, and publishing documentation.

  Rename the CI package-build job to `build-packages` and update its dependent jobs.

### Fixed

- Use a shared 15-second startup default, enforce configuration and readiness deadlines, and report safe startup failure reasons without retaining private exception details.
- Keep graph file requests independent of telemetry acknowledgements, drain accepted requests before closing modules, and prevent older attempts from replacing newer graph publication status documents.
- Preserve valid stage identifiers unchanged in telemetry events and heartbeats. Reject malformed stage identifiers before persistence while continuing to redact credentials from messages and details.
- Preserve validation-refused build outcomes and caller-supplied failure codes in the telemetry service. Allow shared-host cleanup to wait until graph publication finishes. Retain only rejected producers' records locally after permanent collector HTTP client errors, and test these behaviors against the installed package and collector.
- Keep graph publication running when a preserved directory is removed or the local queue reports an error, and record the latest status only after the queue commit.
- Move stage tracking, event construction, and credential redaction into the separate local telemetry emitter service. Keep the build adapter limited to process startup, request serialization, socket transport, and shutdown. Commit all events from each request atomically before acknowledging it.

  Wait for a successful socket ping before runtime tests send requests, and clean up the service when test setup fails. Cover delayed listening and failed initialization with regression tests.

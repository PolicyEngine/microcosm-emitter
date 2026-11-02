# Development instructions

The Rules section below is generated from `rules/registry.toml`. Change a rule
in the registry, never in this file.

## Rules

<!-- BEGIN GENERATED: rules (python -m scripts.rules render) -->

Every rule for this repository lives in `rules/registry.toml` and is rendered,
with its enforcement and gaps, in `docs/generated/rules.md`. Blocking rules
fail CI. Doc-only rules are not checked automatically, so follow them yourself.
To add or change a rule, read `docs/engineering/skills/rules-registry.md`.

### Runtime and package boundaries

- `emitter.boundaries.graph-adapter-imports-no-service-code`: Graph adapters, the Orrery client and its contracts, import no service, database, authentication or HTTP code: inventory hashing, durable enqueue, receipt lookup and retries run in the separate local emitter service, and the client never falls back to writing the database directly. (blocking: pytest `tests/orrery/test_client.py`, `tests/test_boundaries.py`)
- `emitter.boundaries.host-imports-no-domain-code`: The generic runtime, microcosm_emitter.host, imports no telemetry, Orrery, database or authentication code, no Microcosm, SQLAlchemy, Alembic or Hugging Face Hub module, and not the interface package. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.interface-imports-only-the-standard-library`: The interface package imports only the standard library and itself, and importing it loads no microcosm_emitter module. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.public-credential-lookup`: Code reads the Hugging Face credential through core.auth.huggingface_token; no module imports the private _huggingface_token. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.shared-definitions-written-once`: What both packages use is defined once: the emitter's in-process clients reuse the interface's TelemetryRun, wire_value, Orrery publication types and default_spool_path rather than copies, and each hand-over failure code and limit the packages share is assigned at module level exactly once across both. (blocking: pytest `tests/interface/test_in_process_clients.py`, `tests/test_boundaries.py`)
- `emitter.boundaries.telemetry-client-imports-no-service-code`: The telemetry client, microcosm_emitter.telemetry outside its service package, imports no service implementation, database or authentication module, and importing the public modules opens no socket, starts no process and creates no directory. (blocking: pytest `tests/test_boundaries.py`)

### Databases and migrations

- `emitter.persistence.no-raw-sql-or-schema-calls`: No module under src/microcosm_emitter calls a function or method named create_all, exec_driver_sql, executescript or text, or imports sqlite3, psycopg or psycopg2 in the form import <module>. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.persistence.sqlalchemy-and-alembic-only`: Python databases use the SQLAlchemy declarative ORM and Alembic only: no handwritten SQL or direct-driver persistence in any form, no schema repair and no adoption of databases made before Alembic. (doc-only, not checked: follow it yourself)
- `emitter.persistence.typescript-uses-drizzle`: TypeScript database code, if any is added, uses Drizzle ORM and Drizzle Kit migrations. (doc-only, not checked: follow it yourself)

### Distributions and releases

- `emitter.distributions.artifacts-verified`: Every pull request builds both distributions, each wheel from its source archive, and checks them: twine check --strict passes; tools/check-installed.py finds one wheel and one source archive per distribution, refuses a source archive holding the rules tool, and installs the wheels into two clean environments outside the checkout without Hugging Face credentials; in the emitter's, Microcosm is absent, the project URL is the repository, importing the host and the Orrery client loads no database, Hugging Face, telemetry or service module, and telemetry-only, graph-only, combined and failing services start and record their events and graph jobs in an event queue database at Alembic revision 20261008_02; in a build's, which holds only the interface, the interface has no requirements and ships its protocol fixtures, and the installed emitter it starts reports the interface's version and the telemetry, Orrery and Hugging Face modules, accepts telemetry, an Orrery hand-over and a run-file staging request, and exits with status 0. (blocking: CI job `build-packages`, `tests`)
- `emitter.distributions.exactly-two-at-one-version`: The repository publishes exactly two distributions, always together and at one version: microcosm-emitter, which installs the generic host and its required telemetry with no extras, and the dependency-free microcosm-emitter-interface, which microcosm-emitter==X requires at ==X; there are no standalone telemetry packages, optional telemetry installs or further distributions. (blocking: pytest `tests/test_package_names.py`)
- `emitter.distributions.lock-only-merges-release-nothing`: A Dependabot pull request that changes only uv.lock needs no changelog fragment, since it changes no published package's requirements, and a merge without fragments releases nothing: tools/update-version.sh changes no file and push.yaml makes no version commit, so nothing is tagged or published. (blocking: pytest `tests/test_dependabot.py`)
- `emitter.distributions.one-build-script`: Only tools/check-artifacts.sh builds the distributions, with one uv build --all-packages into a dist directory it removes beforehand and no --clear, no workflow runs uv build itself, and tools/check-installed.py checks the interface wheel apart from the emitter. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.release-credentials`: In push.yaml only the publish job, in the pypi environment, may request an OpenID Connect token, and the workflow reads no secrets but APP_ID and APP_PRIVATE_KEY, once each, for the GitHub App token that commits the version. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.release-order`: push.yaml releases a merge to main in order (the version commit, then ci.yml on that commit, then the tag, then publication) and publishes to PyPI only the artifacts ci.yml built, one per distribution, the interface first, skipping files PyPI already holds so a rerun completes a partial upload. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.source-archives-exclude-the-rules-tool`: The microcosm-emitter source archive leaves out the rules tool, the registry and their documents, and tools/check-installed.py refuses either source archive if it holds any of them, naming the same paths the build excludes. (blocking: pytest `tests/test_source_archives.py`)
- `emitter.distributions.version-from-fragments`: A release's version bump follows its changelog fragment types (breaking is a major bump, added or removed a minor bump, anything else a patch bump) and rewrites both distributions' versions and the interface pin together, and the release refuses diverged versions, a different pin, missing or untyped fragments, and a missing or repeated version line. (blocking: pytest `tests/test_workflows.py`)

### Collector protocol

- `emitter.protocol.collector-contract`: The installed wheels deliver telemetry to the collector at the commit tools/check-collector.sh pins, with a disposable PostgreSQL database: events become dashboard documents, rejected identities register nothing, a lost acknowledgement is retried without duplicates, and blocked outcomes and supplied failure codes survive. (blocking: pytest `tests/test_collector_contract.py`)
- `emitter.protocol.every-fixture-sent-and-answered`: Every protocol fixture has an interface test that produces it, from a typed call, a module configuration or a host request, and this emitter answers each one: the telemetry, Orrery and Hugging Face modules handle their message and query fixtures, the installed command starts from the launch fixture and answers hello with at least the recorded keys, and the host routes each envelope fixture to its module. (blocking: pytest `tests/interface/test_client_messages.py`, `tests/interface/test_host_protocol.py`, `tests/interface/test_in_process_clients.py`, `tests/interface/test_protocol_fixtures.py`)
- `emitter.protocol.no-hosted-events-api`: Preserve the existing collector protocol, and do not add a general hosted events API. (doc-only, not checked: follow it yourself)
- `emitter.protocol.released-fixtures-unchanged`: A released protocol fixture is never edited or removed, since each one's SHA-256 is pinned, and every fixture's protocol version is one this emitter accepts. (blocking: pytest `tests/interface/test_protocol_fixtures.py`)
- `emitter.protocol.trusted-collector-origins`: Every trusted collector origin is a bare HTTPS origin that a module configuration selects as itself, with or without a trailing slash. (blocking: pytest `tests/test_collector_origin.py`)

### Test isolation

- `emitter.tests.no-ambient-credentials-or-hosted-collector`: Tests never use ambient Hugging Face credentials, and never send a telemetry or authentication request to a hosted collector: an autouse fixture in conftest.py removes the Hugging Face credential variables, gives each test a temporary Hugging Face home, and fails a test whose telemetry or authentication request names a host other than loopback. (blocking: CI job `tests`)
- `emitter.tests.real-migrations-synthetic-credentials`: Tests use temporary databases created through real migrations, synthetic credentials and loopback network services, and contact no hosted collector through any client, including the HTTP clients of the submissions client and the Orrery queue, which conftest.py does not intercept. (doc-only, not checked: follow it yourself)

### Code quality

- `emitter.quality.python-file-length`: No tracked Python file has more than 800 lines, or more than 1,500 if it is test code: a file under tests/ or under the rules tool's tests and fixtures, scripts/rules/tests/. (blocking: quality guard `file-length`)
- `emitter.quality.ruff-format`: All Python is formatted with ruff format. (blocking: ruff format)
- `emitter.quality.ruff-lint`: All Python passes ruff check with the rule families E, F, I, N, W, UP and B selected, except E501 (line length), which ruff format settles. (blocking: ruff check (B, E, F, I, N, UP, W))
- `emitter.quality.rules-tool-tests-pass`: The rules tool's own tests, the unittest tests under scripts/rules/tests, pass in pr.yaml's rules job, on Ubuntu with Python 3.13 and only the standard library. (blocking: CI job `rules`)
- `emitter.quality.tests-pass`: Every pytest test under tests/ except the collector contract test passes in ci.yml's tests job, on Ubuntu and macOS with Python 3.13 and 3.14, in the environment uv sync --locked installs. (blocking: CI job `tests`)
- `emitter.quality.toolchain`: Use uv, Python 3.13 or later, pytest and Ruff. (doc-only, not checked: follow it yourself)

### Dependency locking

- `emitter.dependencies.dependabot-relock`: Dependabot proposes uv.lock updates every week, for the uv ecosystem at the repository root, as one grouped pull request that changes only uv.lock (versioning-strategy lockfile-only); it runs on GitHub's side with its own credentials, so no secret of this repository reaches the code resolving runs. (blocking: pytest `tests/test_dependabot.py`)
- `emitter.dependencies.install-from-lock`: Every install of the project's Python dependencies, in workflows, the shell scripts under tools/ they run and the commands the Python scripts under tools/ start, installs exactly the locked versions (uv sync --locked, uv run --locked); any other install is a registered exception for that one site. (blocking: quality guard `locked-installs`)
- `emitter.dependencies.lock-committed`: Every project directory with a pyproject.toml commits a uv.lock that matches it, or is a member of the uv workspace whose uv.lock locks it, and a pull-request job checks the lock with uv lock --check. (blocking: quality guard `locked-installs`)
- `emitter.dependencies.test-from-lock`: Every test command (pytest, python -m unittest) in workflows, the shell scripts under tools/ they run and the commands the Python scripts under tools/ start runs through uv run --locked, in the locked environment; any other test run is a registered exception for that one site. (blocking: quality guard `locked-installs`)
- `emitter.dependencies.uv-version-pinned`: The one uv release is written once, as the line uv==X.Y.Z in .github/uv-version.txt: each astral-sh/setup-uv step reads it with version-file and writes no version:, any uv container image, install script, release download or package install of uv names it, and the root pyproject.toml's [tool.uv] required-version is a lower bound (>=X.Y) it meets, so an older uv refuses the project while Dependabot's uv still runs. (blocking: quality guard `uv-version`)

### CI workflows and scripts

- `emitter.ci.multiline-run-is-script`: Every workflow run: step holds one command: one line of code, with no commands chained by &&, || or ;. (blocking: quality guard `ci-scripts`)
- `emitter.ci.pull-request-workflow-structure`: pr.yaml runs on pull requests and calls ci.yml, the changelog check and the rules registry, and its Required checks job runs always and needs each other job; ci.yml is called only by other workflows and has exactly the quality, tests, build-packages and collector jobs, testing on Ubuntu and macOS with Python 3.13 and 3.14, with the collector job after build-packages; pr-description.yaml runs for open pull requests whose base, or whose stack's final target, is main, on every event that can change the description, with read-only permissions, in one job named PR description. (blocking: pytest `tests/test_workflows.py`)
- `emitter.ci.required-checks`: Every pull request runs the verification workflow, ci.yml, and Required checks, a status check main's ruleset requires, passes only when every other pr.yaml job succeeded. (blocking: CI job `verify`, `required`)
- `emitter.ci.shell-logic-in-tools-scripts`: Shell logic of several commands lives in a Bash script under tools/, which a workflow step calls with a single command, and a test runs that script, with fake commands in place of anything it would change or reach outside the test. (doc-only, not checked: follow it yourself)
- `emitter.ci.shell-scripts-parse`: Every shell script under tools/ parses (bash -n). (blocking: CI job `quality`)
- `emitter.ci.shell-scripts-pass-shellcheck`: Every shell script under tools/ passes shellcheck, which the Rules registry job installs as shellcheck-py 0.11.0.1. (blocking: quality guard `ci-scripts`)
- `emitter.ci.workflow-scripts-tested`: Every shell script under tools/ that a workflow calls is named in a test file under tests/. (blocking: quality guard `ci-scripts`)
- `emitter.ci.workflows-pass-actionlint`: Every workflow passes actionlint 1.7.12, which also runs shellcheck on each run: step. (blocking: CI job `lint-workflows`)

### Process and repository hygiene

- `emitter.agents.adapters-point-to-agents-md`: CLAUDE.md only points to AGENTS.md; guidance and rules are written in AGENTS.md alone. (blocking: quality guard `rules-registry`)
- `emitter.process.changelog-fragment`: Every pull request adds at least one changelog fragment, changelog.d/<name>.<type>.md with a type of breaking, added, changed, fixed or removed, and keeps both distributions' versions and the interface pin consistent, so its merge can be released; a Dependabot pull request that changes only uv.lock is the exception. (blocking: CI job `changelog`)
- `emitter.process.feature-branches-and-pull-requests`: Push feature branches and open pull requests; never push to main directly, merge automatically or monitor queued checks. (doc-only, not checked: follow it yourself)
- `emitter.process.pr-tracking-issue`: The first line of the description of every open pull request whose base, or whose stack's final target, is main is Fixes #ISSUE_NUMBER, naming an open issue, not a pull request, in this repository; a pull request Dependabot opens is the exception. (blocking: CI job `pr-description`)
- `emitter.process.rules-registry`: Every rule is registered in rules/registry.toml, is enforced where it claims to be, and its rendered views are current. (blocking: quality guard `rules-registry`)

<!-- END GENERATED: rules -->

# Microcosm emitter interface

`microcosm-emitter-interface` is the small, dependency-free package a Microcosm build imports to hand work to the separately installed `microcosm-emitter`. Microcosm decides what to hand over; the emitter decides where it goes.

Install the emitter on its own, outside the build's environment:

```bash
uv tool install microcosm-emitter
```

The build then depends on this package with a version range and starts the emitter through it:

```python
from microcosm_emitter_interface import EmitterClient, TelemetryRun, TelemetrySettings

client = EmitterClient.start(
    telemetry=TelemetrySettings(run=TelemetryRun("run-id", "UK", "uk-build")),
)
client.telemetry.transition_stage("calibrating")
client.telemetry.complete()
client.close()
```

`EmitterClient.start` runs the `microcosm-emitter` command as the build's own child process. The command comes from the `command` argument, then the `MICROCOSM_EMITTER_COMMAND` environment variable, then `PATH`. The child inherits the build's environment variables and user, so it finds the same Hugging Face credential: `HF_TOKEN`, then `HUGGINGFACE_TOKEN`, then the token saved by `hf auth login`.

No call raises into the build: not because the emitter is missing, slow or refusing a request, and not because an argument cannot be converted to JSON (for example a multi-element NumPy array or a self-referencing mapping). Each call converts its arguments inside its error handling and reports whether the emitter accepted it; after a refusal, `client.last_failure` says why (`emitter_unavailable`, `invalid_arguments` or `not_accepted`). Startup settings that cannot be converted start an unavailable client (`invalid_configuration`). The build continues in every case. Constructing a request object (for example `StageRunFiles(...)`) that breaks the shared path and identifier rules raises `ValueError` in the build's own code, before any call.

## Calls

| Hand-over | Call | Emitter module |
|---|---|---|
| Telemetry event | `client.telemetry.stage(...)`, `transition_stage`, `progress`, `calibration_progress`, `fail`, `refuse`, `complete`, `emit` (each one `emit_telemetry`) | `telemetry` |
| Staging run files under `runs/<run_id>/` | `stage_run_files(StageRunFiles(...))`, `run_files_receipt`, `read_back_run_files` | `huggingface` |
| Staged dataset bundle, each file with its role | `stage_dataset_bundle(StageDatasetBundle(...))`, `dataset_bundle_receipt`, `check_write_access` | `huggingface` |
| What the collector made of a bundle, release publication, promotion or withdrawal that carries `CollectorProvenance`: each submission's status, the collector's reason for a rejection, and its receipt (answers at once; the emitter checks in the background). Submitted only with `HuggingFaceSettings(collector_submissions=True)` | `collector_submission_receipt` | `huggingface` |
| Release publication: release, build, whether current, files with roles, and optional `CollectorProvenance`; promotion and withdrawal, each with optional `CollectorProvenance`, which sends the new `releases.json` to the collector | `publish_release(PublishRelease(...))`, `promote_build`, `withdraw_build`, `release_receipt` | `huggingface` |
| Orrery inputs | `orrery_inventory`, `hand_over_orrery_inputs`, `orrery_receipt`, `retry_orrery_inputs` | `orrery` |

## Emitter commands

Three calls run one of the installed emitter's commands to its exit, without starting the emitter's host, and return the parsed answer or a `CommandFailure`. None raises into the build. Each finds the command as `EmitterClient.start` does, runs it with standard input reading nothing (`/dev/null`), and takes `command`, `timeout` (at most a day, `MAX_TIMEOUT_SECONDS`) and `environment`: the whole environment the command runs with, or without it the build's own.

| Call | Command | Answer | Failures for its own exit codes |
|---|---|---|---|
| `emitter_version()` | `microcosm-emitter version` | `EmitterVersion`: `emitter_version`, `protocol_versions` and `collector_origin`. Any JSON object is an answer, with each field None when it is missing or of another type, so a build can still name an emitter too old for it (`collector_origin` is printed from 0.11.0). | none besides `usage_error` |
| `emitter_status(event_queue_file=None)` | `microcosm-emitter status` | `EmitterStatus`: one `EventQueueRun` (`run_id`, `producer_id`, `upload_state`, `reason`, `undelivered_events`) per printed run, in its order. Without `event_queue_file`, the command reads the default path of the environment it runs in. | `no_event_queue_file` (8), `unusable_event_queue_file` (9) |
| `telemetry_token(lifetime_seconds)` | `microcosm-emitter telemetry-token` | `TelemetryToken`: `access_token`, `expires_at` (ISO 8601 with a time zone, also as `expiry`) and `collector_origin` (HTTPS). Its repr leaves the token out. `development_collector_url` asks a loopback collector, for tests. | `missing_credential` (3), `rejected_credential` (4), `collector_unavailable` (5), `lifetime_unsupported` (6), `unexpected_failure` (7) |

Every call can also fail with `invalid_arguments` (nothing was run), `command_not_found`, `could_not_run` (`error_type` names the exception), `timed_out` (the command was stopped), `usage_error` (exit 2, which an emitter without the command also gives), `exited` (an exit code the command does not document, or a signal) and `unusable_output` (exit 0, but not the documented answer; `output_problem` says what is wrong without quoting it). A failure's `message` is the last line the command wrote on standard error, at most 300 characters; it never comes from standard output, and a `telemetry-token` that printed anything there gets none.

Each answer has a field for every key the emitter's README documents for its command and ignores the others, so an emitter that prints more still parses. `fixtures/commands/` records each command's output, which `microcosm_emitter_interface.commands.recorded_command_output(name)` returns for tests that stand in for the emitter. The emitter's CI checks that each command prints exactly the recorded keys, and that each call parses its recording and the real command's answer.

`microcosm_emitter_interface.releases` holds the shape of `releases.json` and the checks every release write must pass, so Microcosm, the emitter and a scheduled repository check run the same code.

## Compatibility

Both packages are released together with one version number, and `microcosm-emitter==X` requires `microcosm-emitter-interface==X`. A build, though, may run with a different emitter from the interface it depends on: it keeps the emitter it started with, and the next build uses whichever emitter is installed then. So the socket protocol carries its own version:

- `PROTOCOL_VERSION` names the shape of every message this package sends. The emitter lists the versions it accepts in its `hello` answer, and the client refuses an emitter that does not list its version (`unsupported_protocol_version`); the build continues without it.
- **Every emitter release must accept every protocol version that interface releases still in use send.** Adding a module or a message kind keeps the version, and so does adding an optional field to a message that older emitters ignore, with a new fixture beside the message's released one (as `outer` in `telemetry-progress-with-outer.json`). Changing or removing a field of an existing message needs a new version, which emitters then accept alongside the old one.
- `fixtures/protocol-v<N>/` holds one JSON file per message of protocol version `N`, including how the command is started (`host-launch.json`: its arguments and the configuration object on standard input) and each socket request's whole envelope (`host-ping.json`, `host-message-envelope.json`, `host-query-envelope.json`, `host-close.json`, and `host-hello.json`). This package's tests check that each typed call, the launcher and the socket transport send exactly their fixture, and the emitter's contract tests check that it accepts every fixture of every version it supports, so a change to the command's arguments or the envelope cannot pass by changing both sides at once. An emitter's `hello` answer must contain the recorded keys and may add more. A released fixture is never edited or removed: the emitter's tests pin each one's SHA-256.
- An emitter that lacks a module this package asks for starts without it (`--skip-unavailable-modules`); calls for that module report that they were not accepted.

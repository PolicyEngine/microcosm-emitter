"""Command-line entry for the generic service host."""

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from microcosm_emitter.compatibility import EmitterDescription, HelloResponse
from microcosm_emitter.host.constants import MAX_MESSAGE_BYTES
from microcosm_emitter.host.contracts import ModuleContext, ServiceModule
from microcosm_emitter.host.errors import ModuleNotInstalledError, safe_startup_error
from microcosm_emitter.host.loading import load_module
from microcosm_emitter.host.process import parent_process
from microcosm_emitter.host.runtime import ServiceRuntime


def _read_configurations() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
    if len(payload) > MAX_MESSAGE_BYTES:
        error_message = "configuration is too large"
        raise ValueError(error_message)
    configurations = json.loads(payload)
    if not isinstance(configurations, dict) or not configurations:
        error_message = "module configurations must be a nonempty object"
        raise ValueError(error_message)
    return configurations


def _load_modules(
    configurations: dict[str, Any],
    context: ModuleContext,
    *,
    skip_unavailable: bool,
) -> tuple[dict[str, ServiceModule], list[str]]:
    modules = {}
    skipped = []
    for name, settings in configurations.items():
        if not isinstance(name, str) or not isinstance(settings, dict):
            error_message = "invalid module configuration"
            raise TypeError(error_message)
        try:
            modules[name] = load_module(name, settings, context)
        except ModuleNotInstalledError:
            if not skip_unavailable:
                raise
            skipped.append(name)
    if not modules:
        error_message = "no requested module is installed"
        raise ValueError(error_message)
    return modules, skipped


def main(
    argv: list[str] | None = None,
    *,
    description: EmitterDescription | None = None,
) -> int:
    """Run the host.

    With ``description`` (given only by `microcosm-emitter host`), the host
    answers ``hello`` with it and the loaded and skipped modules. Without it,
    as when started with `python -m microcosm_emitter.host`, ``hello`` is
    refused like any unknown action.
    """
    parser = argparse.ArgumentParser(prog="microcosm-emitter host")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    # Earlier launchers pass the parent's creation time. Without it, the host
    # reads it from its own parent, which must be the process named here.
    parser.add_argument("--parent-created-at", type=float)
    parser.add_argument(
        "--skip-unavailable-modules",
        action="store_true",
        help="Start without requested modules that this emitter does not install.",
    )
    args = parser.parse_args(argv)
    try:
        configurations = _read_configurations()
        parent = parent_process(args.parent_pid, args.parent_created_at)
        context = ModuleContext(args.parent_pid)
        modules, skipped = _load_modules(
            configurations,
            context,
            skip_unavailable=args.skip_unavailable_modules,
        )
        hello_response: HelloResponse | None = (
            None
            if description is None
            else {
                "emitter_version": description["emitter_version"],
                "protocol_versions": list(description["protocol_versions"]),
                "modules": sorted(modules),
                "skipped_modules": sorted(skipped),
            }
        )
        ServiceRuntime(
            args.socket,
            modules=modules,
            is_parent_alive=parent.alive,
            description=hello_response,
        ).run()
    # Any failure exits 1 with only safe_startup_error's fixed reason, never the
    # exception's details.
    except Exception as error:  # noqa: BLE001
        # Never include configuration or module exception payloads in stderr.
        print(str(safe_startup_error(error)), file=sys.stderr)
        return 1
    else:
        return 0
    finally:
        with contextlib.suppress(OSError):
            args.socket.parent.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())

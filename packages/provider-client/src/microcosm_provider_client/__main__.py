"""Command-line entry for the generic service host."""

import argparse
import json
import sys
from pathlib import Path

from microcosm_provider_client.contracts import ModuleContext
from microcosm_provider_client.loading import load_module
from microcosm_provider_client.process import ParentProcess
from microcosm_provider_client.runtime import ServiceRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--module")
    selection.add_argument("--modules-json")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--config-json", default="{}")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created-at", type=float, required=True)
    args = parser.parse_args()
    try:
        configuration = json.loads(args.config_json)
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        parent = ParentProcess(args.parent_pid, args.parent_created_at)
        configurations = (
            json.loads(args.modules_json)
            if args.modules_json
            else {args.module: configuration}
        )
        if not isinstance(configurations, dict) or not configurations:
            raise ValueError("modules must be a non-empty object")
        context = ModuleContext(args.parent_pid)
        modules = {}
        for name, settings in configurations.items():
            if not isinstance(name, str) or not isinstance(settings, dict):
                raise ValueError("invalid module configuration")
            modules[name] = load_module(name, settings, context)
        ServiceRuntime(args.socket, modules=modules, is_parent_alive=parent.alive).run()
        return 0
    except Exception as error:
        # Never include configuration or module exception payloads in stderr.
        print(f"Local service failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    finally:
        try:
            args.socket.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

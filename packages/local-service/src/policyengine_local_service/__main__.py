"""Command-line entry for the generic service host."""

import argparse
import json
import sys
from pathlib import Path

from policyengine_local_service.contracts import ModuleContext
from policyengine_local_service.loading import load_module
from policyengine_local_service.process import ParentProcess
from policyengine_local_service.runtime import ServiceRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created-at", type=float, required=True)
    args = parser.parse_args()
    try:
        configuration = json.loads(args.config_json)
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        parent = ParentProcess(args.parent_pid, args.parent_created_at)
        module = load_module(args.module, configuration, ModuleContext(args.parent_pid))
        ServiceRuntime(args.socket, module, is_parent_alive=parent.alive).run()
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

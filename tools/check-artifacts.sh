#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# uv builds each wheel from its sdist, checking that source archives are complete.
# Clear once before either package writes artifacts to the shared directory.
uv build --package microcosm-provider-client --out-dir dist --clear
uv build --package microcosm-provider-telemetry --out-dir dist
uv run --no-sync twine check --strict dist/*
uv run --no-sync python tools/check-installed.py

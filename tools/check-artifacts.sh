#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Build the wheel from its source archive to check that both libraries and
# Alembic migrations are included. Remove artifacts from earlier builds first.
uv build --out-dir dist --clear
uv run --no-sync twine check --strict dist/*
uv run --no-sync python tools/check-installed.py

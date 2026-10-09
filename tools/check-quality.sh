#!/usr/bin/env bash
set -euo pipefail

uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
for script in tools/*.sh; do
  bash -n "$script"
done

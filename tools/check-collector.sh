#!/usr/bin/env bash
set -euo pipefail

: "${TEST_DATABASE_URL:?Set the disposable PostgreSQL test database URL}"
expected_commit=80a2a4ac7654f586b2b4b8d4c6ec7d2379616722
test "$(git -C .collector rev-parse HEAD)" = "$expected_commit"
uv sync --locked
uv pip install -e .collector/telemetry-service
uv pip install --reinstall --no-deps dist/*.whl
# --no-sync is necessary: these tests must use the installed wheels, not workspace sources.
uv run --no-sync pytest tests/test_collector_contract.py

#!/usr/bin/env bash
set -euo pipefail

: "${RELEASE_TAG:?Missing GitHub release tag}"
test "${RELEASE_PRERELEASE:-false}" = false
git fetch origin main
git merge-base --is-ancestor HEAD origin/main
uv run --no-project python tools/check-release.py "$RELEASE_TAG"

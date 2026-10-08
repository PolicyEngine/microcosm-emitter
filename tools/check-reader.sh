#!/usr/bin/env bash
set -euo pipefail
cd typescript/orrery
bun install --frozen-lockfile
bun run format:check
bun run typecheck
bun test
bun run build
bun pm pack --destination ../../reader-dist
cd ../..
bun tools/check-reader-installed.ts

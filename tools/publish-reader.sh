#!/usr/bin/env bash
set -euo pipefail
node -e 'const [major, minor, patch] = require("child_process").execFileSync("npm", ["--version"], {encoding:"utf8"}).trim().split(".").map(Number); if (major < 11 || (major === 11 && (minor < 5 || (minor === 5 && patch < 1)))) throw new Error("npm OIDC requires npm >=11.5.1");'
shopt -s nullglob
archives=(reader-dist/*.tgz)
if (("${#archives[@]}" != 1)); then
  echo "Expected exactly one verified reader tarball." >&2
  exit 1
fi
npm publish --access public --provenance "${archives[0]}"

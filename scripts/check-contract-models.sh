#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

uv run --script "${repository_root}/scripts/generate-contract-models.py" --self-test
uv run --script "${repository_root}/scripts/generate-contract-models.py" --check
if ! cmp -s \
  "${repository_root}/packages/typescript-browser-client/src/contracts.ts" \
  "${repository_root}/packages/typescript-server-client/src/contracts.ts"; then
  echo "The browser and server contract validators differ." >&2
  exit 1
fi

echo "Contract model checks passed."

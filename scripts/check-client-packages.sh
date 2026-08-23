#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

removed_paths=(
  apps/embed-example
  packages/python-client
  packages/typescript-browser-client
  packages/typescript-server-client
)

for removed_path in "${removed_paths[@]}"; do
  if [[ -e "${removed_path}" ]] || [[ -L "${removed_path}" ]]; then
    echo "A removed product path remains: ${removed_path}" >&2
    exit 1
  fi
done

if rg -n \
  -e 'apps/embed-example' \
  -e 'packages/python-client' \
  -e 'packages/typescript-browser-client' \
  -e 'packages/typescript-server-client' \
  -e '@llmrouter/embed-example' \
  -e '@llmrouter/browser-client' \
  -e '@llmrouter/server-client' \
  -e 'llmrouter-client' \
  package.json package-lock.json pyproject.toml uv.lock tsconfig.json; then
  echo "A workspace manifest still names a removed product." >&2
  exit 1
fi

echo "Client package reset checks passed."

#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

removed_paths=(
  apps/admin/src/EmbedFrame.tsx
  apps/admin/src/embedProtocol.ts
  apps/admin/src/embedSnapshotLoader.ts
  apps/admin/src/embedStyles.css
  apps/admin/test/embedProtocol.test.ts
  apps/embed-example
  packages/python-client
  packages/typescript-browser-client
  packages/typescript-server-client
  scripts/check-contract-models.sh
  scripts/generate-contract-models.py
)

for removed_path in "${removed_paths[@]}"; do
  if [[ -e "${removed_path}" ]] || [[ -L "${removed_path}" ]]; then
    echo "A removed product path remains: ${removed_path}" >&2
    exit 1
  fi
done

reference_paths=(
  .prettierignore
  apps/admin/src/main.tsx
  apps/admin/test/App.test.tsx
  apps/admin/vite.config.ts
  docker-compose.dev.yml
  docs/api/README.md
  eslint.config.mjs
  package.json
  package-lock.json
  pyproject.toml
  scripts/check-api-contracts.sh
  scripts/check-contract-digests.sh
  scripts/check-local-development.py
  scripts/check-node.sh
  scripts/check-python.sh
  scripts/check-repository.sh
  scripts/generate-contract-digests.py
  scripts/local-development-bootstrap.py
  scripts/local-development-live-openrouter.py
  scripts/local-development.sh
  scripts/tests/test_local_development.py
  tsconfig.json
  uv.lock
)

for reference_path in "${reference_paths[@]}"; do
  if [[ ! -f "${reference_path}" ]]; then
    echo "A product reset reference file is missing: ${reference_path}" >&2
    exit 1
  fi
done

if grep -nE \
  'apps/embed-example|packages/(python-client|typescript-browser-client|typescript-server-client)|@llmrouter/(embed-example|browser-client|server-client)|llmrouter-client|check-contract-models|generate-contract-models|generated[-_]models|EmbedFrame|embedProtocol|embedSnapshotLoader|embedStyles|hostProtocol|hostApi|example-host-token|LLMROUTER_EXAMPLE_|service-administration' \
  "${reference_paths[@]}"; then
  echo "A build, manifest, or local tool still names a removed product." >&2
  exit 1
fi

echo "Client package reset checks passed."

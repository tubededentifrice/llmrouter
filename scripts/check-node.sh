#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

if [[ "$(node --version)" != "v24.17.0" ]]; then
  echo "Node.js 24.17.0 is required." >&2
  exit 1
fi
if [[ "$(npm --version)" != "11.18.0" ]]; then
  echo "npm 11.18.0 is required." >&2
  exit 1
fi

npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
"${repository_root}/scripts/check-client-packages.sh"
npm run security
react_report="$(mktemp)"
embed_react_report="$(mktemp)"
trap 'rm -f "${react_report}" "${embed_react_report}"' EXIT
npm run --silent react-doctor >"${react_report}"
node scripts/check-react-doctor.mjs "${react_report}"
npm run --silent react-doctor:embed-example >"${embed_react_report}"
node scripts/check-react-doctor.mjs "${embed_react_report}"
if node scripts/check-react-doctor.mjs \
  scripts/tests/fixtures/react-doctor-invalid.json >/dev/null 2>&1; then
  echo "The React Doctor expected-failure fixture passed unexpectedly." >&2
  exit 1
fi

echo "Node checks passed."

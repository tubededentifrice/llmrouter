#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

mapfile -t manifests < <(
  find apps packages -path '*/node_modules' -prune -o \
    \( -name package.json -o -name pyproject.toml \) -print | sort
)
uv run --no-project --python 3.13.12 python \
  scripts/check-dependency-policy.py --require-root-policy \
  package.json pyproject.toml "${manifests[@]}"

invalid_fixtures=(
  "scripts/tests/fixtures/dependency-policy-invalid/package.json"
  "scripts/tests/fixtures/dependency-policy-invalid/pyproject.toml"
)
for invalid_fixture in "${invalid_fixtures[@]}"; do
  if uv run --no-project --python 3.13.12 python \
    scripts/check-dependency-policy.py "${invalid_fixture}" >/dev/null 2>&1; then
    echo "The dependency policy expected-failure fixture passed: ${invalid_fixture}" >&2
    exit 1
  fi
done

echo "Dependency policy fixture checks passed."

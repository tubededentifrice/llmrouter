#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 0 ]]; then
  echo "The Python style check does not accept arguments." >&2
  exit 2
fi

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

python_targets=(
  packages
  scripts/check-dependency-policy.py
  scripts/generate-contract-digests.py
)

uv run ruff format --check "${python_targets[@]}"
uv run ruff check "${python_targets[@]}"

echo "Python style checks passed."

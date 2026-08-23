#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 0 ]]; then
  echo "The Python style check does not accept arguments." >&2
  exit 2
fi

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

uv_version="$(uv --version)"
if [[ "${uv_version}" != "uv 0.12.0" && "${uv_version}" != "uv 0.12.0 "* ]]; then
  echo "uv 0.12.0 is required." >&2
  exit 1
fi

python_targets=(
  packages
  scripts/check-dependency-policy.py
  scripts/generate-contract-digests.py
)

uv run ruff format --check "${python_targets[@]}"
uv run ruff check "${python_targets[@]}"

echo "Python style checks passed."

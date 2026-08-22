#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

uv_version="$(uv --version)"
if [[ "${uv_version}" != "uv 0.12.0" && "${uv_version}" != "uv 0.12.0 "* ]]; then
  echo "uv 0.12.0 is required." >&2
  exit 1
fi

uv sync --frozen --all-packages
if [[ "$(uv run python --version)" != "Python 3.14.6" ]]; then
  echo "Python 3.14.6 is required." >&2
  exit 1
fi
uv run ruff format --check packages scripts/check-dependency-policy.py scripts/generate-contract-models.py
uv run ruff check packages scripts/check-dependency-policy.py scripts/generate-contract-models.py
uv run mypy packages scripts/check-dependency-policy.py scripts/generate-contract-models.py
uv run pytest
uv run bandit -q -r \
  packages/backend-role/src \
  packages/python-client/src \
  scripts/check-dependency-policy.py \
  scripts/generate-contract-models.py \
  -x packages/python-client/src/llmrouter_client/generated_models.py
uv run pip-audit --skip-editable
uv build --package llmrouter-backend
uv build --package llmrouter-client

echo "Python checks passed."

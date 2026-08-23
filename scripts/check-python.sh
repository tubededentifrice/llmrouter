#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

"${repository_root}/scripts/check-python-style.sh"
uv sync --frozen --all-packages
if [[ "$(uv run python --version)" != "Python 3.14.6" ]]; then
  echo "Python 3.14.6 is required." >&2
  exit 1
fi
uv run mypy packages scripts/check-dependency-policy.py scripts/generate-contract-digests.py
uv run pytest
uv run bandit -q -r \
  packages/backend-role/src \
  scripts/check-dependency-policy.py \
  scripts/generate-contract-digests.py
uv run pip-audit --skip-editable
uv build --package llmrouter-backend

echo "Python checks passed."

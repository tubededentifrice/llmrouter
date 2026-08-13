#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

uv run --script "${repository_root}/scripts/check-api-contracts.py" --self-test
uv run --script "${repository_root}/scripts/check-api-contracts.py"
"${repository_root}/scripts/check-contract-models.sh"

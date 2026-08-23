#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

uv run --script "${repository_root}/scripts/generate-contract-digests.py" --self-test
uv run --script "${repository_root}/scripts/generate-contract-digests.py" --check

echo "Contract digest checks passed."

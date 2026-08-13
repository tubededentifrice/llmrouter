#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v bd >/dev/null 2>&1; then
  echo "The bd command is required." >&2
  exit 1
fi

bd -C "${repository_root}" ready \
  --exclude-type epic \
  --exclude-label selfreview \
  --exclude-label blocker:human \
  --exclude-label blocker:external \
  --limit 20 \
  --json

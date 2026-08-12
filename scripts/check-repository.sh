#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required_files=(
  "AGENTS.md"
  "README.md"
  "docs/architecture.md"
  "docs/product-direction.md"
  "docs/api/README.md"
  "docs/decisions/README.md"
  "docs/decisions/0001-complete-platform-with-optional-harness.md"
  "docs/decisions/0002-nearest-scope-replaces-assignment-chain.md"
  "docs/decisions/0003-central-service-tree-and-passkey-administration.md"
  "docs/decisions/0004-hosted-frame-and-headless-administration.md"
  "docs/decisions/0005-route-shared-external-tools.md"
  "docs/interviews/architecture-interview.md"
  "docs/research/README.md"
  "docs/research/ontology-administration-alignment-2026-08.md"
  "docs/research/source-services-2026-08.md"
  "docs/specs/README.md"
  "docs/specs/00-product-and-boundaries.md"
  "docs/specs/01-configuration-and-inheritance.md"
  "docs/specs/03-agent-harness-and-tools.md"
  "docs/specs/06-administration-and-shared-interface.md"
  ".beads/README.md"
  ".beads/config.yaml"
  ".beads/metadata.json"
  ".claude/skills/beads/SKILL.md"
  ".claude/skills/llmrouter-specs/SKILL.md"
  ".claude/skills/selfreview/SKILL.md"
  ".dockerignore"
  ".editorconfig"
  ".gitattributes"
  ".github/workflows/repository-checks.yml"
  ".gitignore"
  ".npmrc"
  "renovate.json"
)

for required_file in "${required_files[@]}"; do
  if [[ ! -f "${repository_root}/${required_file}" ]]; then
    echo "Required file is missing: ${required_file}" >&2
    exit 1
  fi
done

if [[ -e "${repository_root}/runtime-data" ]] ||
  [[ -L "${repository_root}/runtime-data" ]]; then
  echo "The repository must not contain runtime-data." >&2
  exit 1
fi

for required_link in ".agents/skills" ".codex/skills"; do
  if [[ ! -L "${repository_root}/${required_link}" ]] ||
    [[ ! -e "${repository_root}/${required_link}" ]]; then
    echo "Required skill link is missing or broken: ${required_link}" >&2
    exit 1
  fi
done

for required_executable in "${repository_root}"/scripts/*.sh; do
  if [[ ! -x "${required_executable}" ]]; then
    echo "Required executable is not executable: ${required_executable}" >&2
    exit 1
  fi
  bash -n "${required_executable}"
done

grep -qx "min-release-age=14" "${repository_root}/.npmrc"
jq -e . "${repository_root}/renovate.json" >/dev/null

if [[ -s "${repository_root}/.beads/issues.jsonl" ]]; then
  echo "Beads planning is disabled. The issue export must stay empty." >&2
  exit 1
fi

if command -v bd >/dev/null 2>&1; then
  beads_items="$(bd -C "${repository_root}" list --all --json --limit 1)"
  jq -e 'type == "array" and length == 0' <<<"${beads_items}" >/dev/null
fi

git -C "${repository_root}" diff --check
git -C "${repository_root}" diff --cached --check

echo "Repository checks passed."

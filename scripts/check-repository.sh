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
  "docs/decisions/0006-controlled-exact-model-diagnostics.md"
  "docs/decisions/0007-router-owned-retries-and-stream-boundary.md"
  "docs/decisions/0008-use-normal-configuration-for-up-to-24-hours.md"
  "docs/decisions/0009-durable-agent-runs-with-asynchronous-checkpoints.md"
  "docs/decisions/0010-use-a-registered-service-tool-gateway.md"
  "docs/decisions/0011-exchange-service-secrets-for-short-lived-tokens.md"
  "docs/decisions/0012-capture-complete-content-by-default-for-public-data.md"
  "docs/decisions/0013-use-editable-retention-defaults.md"
  "docs/decisions/0014-use-local-spools-a-central-ledger-and-object-storage.md"
  "docs/decisions/0015-use-a-native-api-and-openai-compatibility.md"
  "docs/decisions/0016-publish-python-and-typescript-clients.md"
  "docs/decisions/0017-publish-one-image-with-runtime-roles.md"
  "docs/decisions/0018-permit-breaking-changes-before-version-1.md"
  "docs/decisions/0019-use-fsl-1-1-with-an-apache-2-0-future-license.md"
  "docs/decisions/0020-use-a-shared-catalog-with-scoped-provider-instances.md"
  "docs/decisions/0021-fallback-after-candidate-scoped-failures.md"
  "docs/decisions/0022-use-hierarchical-budgets-with-synchronized-prices.md"
  "docs/decisions/0023-use-built-in-encrypted-credential-storage.md"
  "docs/decisions/0024-publish-valid-configuration-saves-immediately.md"
  "docs/decisions/0025-use-leased-allowances-for-distributed-budgets.md"
  "docs/decisions/0026-use-static-ordered-node-lists.md"
  "docs/decisions/0027-use-a-warm-control-plane-standby.md"
  "docs/decisions/0028-ship-only-the-public-data-profile.md"
  "docs/decisions/0029-use-graph-and-inspector-administration.md"
  "docs/decisions/0030-keep-calling-service-work-in-its-repository.md"
  "docs/decisions/0031-use-client-generated-uuidv7-request-identity.md"
  "docs/decisions/0032-retain-terminal-request-recovery-for-24-hours.md"
  "docs/decisions/0033-use-explicit-best-effort-cancellation-states.md"
  "docs/interviews/architecture-interview.md"
  "docs/research/README.md"
  "docs/research/ontology-administration-alignment-2026-08.md"
  "docs/research/source-services-2026-08.md"
  "docs/specs/README.md"
  "docs/specs/00-product-and-boundaries.md"
  "docs/specs/01-configuration-and-inheritance.md"
  "docs/specs/02-routing-failover-and-request-lifecycle.md"
  "docs/specs/03-agent-harness-and-tools.md"
  "docs/specs/04-identity-credentials-and-tool-gateway.md"
  "docs/specs/05-logging-accounting-and-retention.md"
  "docs/specs/06-administration-and-shared-interface.md"
  "docs/specs/07-reliability-deployment-and-operations.md"
  "docs/specs/08-public-interfaces-clients-and-packaging.md"
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

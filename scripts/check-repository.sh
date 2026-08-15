#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required_files=(
  "AGENTS.md"
  "README.md"
  "LICENSE.md"
  "docs/architecture.md"
  "docs/product-direction.md"
  "docs/api/README.md"
  "docs/api/business-tool-gateway.md"
  "docs/api/contract-policy.yaml"
  "docs/api/contract-digests.json"
  "docs/api/cross-service-conformance.md"
  "docs/api/embed-protocol.md"
  "docs/api/errors.md"
  "docs/api/openapi.yaml"
  "docs/api/request-fingerprint.md"
  "docs/api/service-management.md"
  "docs/api/stream-protocol.md"
  "docs/api/fixtures/administration-grant.json"
  "docs/api/fixtures/attachment.json"
  "docs/api/fixtures/business-tool-call.json"
  "docs/api/fixtures/contract-manifest.json"
  "docs/api/fixtures/effective-configuration.json"
  "docs/api/fixtures/health.json"
  "docs/api/fixtures/model-request.json"
  "docs/api/fixtures/service-token.json"
  "docs/api/fixtures/workspace.json"
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
  "docs/decisions/0034-use-local-health-circuits-with-fleet-hints.md"
  "docs/decisions/0035-use-graduated-spool-pressure-shedding.md"
  "docs/decisions/0036-target-five-minute-rto-and-thirty-second-rpo.md"
  "docs/decisions/0037-use-shared-pocket-id-for-human-authentication.md"
  "docs/decisions/0038-process-authorized-service-data-with-normal-capture.md"
  "docs/decisions/0039-let-services-manage-router-workspace-scopes.md"
  "docs/decisions/0040-limit-cancellation-reconciliation-to-ten-minutes.md"
  "docs/decisions/0041-allow-24-hour-service-secret-rotation-overlap.md"
  "docs/decisions/0042-retain-agent-and-business-tool-audit-for-thirty-days.md"
  "docs/decisions/0043-use-python-backends-and-react-typescript-frontends.md"
  "docs/decisions/0044-use-a-postgresql-fastapi-and-vite-foundation.md"
  "docs/decisions/0045-use-source-driven-adapters-with-registered-contracts.md"
  "docs/decisions/0046-use-least-privilege-grants-and-global-secret-custody.md"
  "docs/decisions/0047-use-one-currency-per-hard-budget-scope.md"
  "docs/decisions/0048-use-immutable-attachments-and-explicit-compatibility-diagnostics.md"
  "docs/decisions/0049-proxy-protected-exports-and-version-operations.md"
  "docs/decisions/0050-use-bounded-attempt-timeouts-and-node-draining.md"
  "docs/decisions/0051-use-daily-backups-with-point-in-time-recovery.md"
  "docs/decisions/0052-use-structured-secret-fields-and-standard-endpoint-trust.md"
  "docs/decisions/0053-allow-two-early-security-fix-pins.md"
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
  ".claude/skills/director/SKILL.md"
  ".claude/skills/director/agents/openai.yaml"
  ".claude/skills/llmrouter-specs/SKILL.md"
  ".claude/skills/repository-tooling/SKILL.md"
  ".claude/skills/repository-tooling/agents/openai.yaml"
  ".claude/skills/selfreview/SKILL.md"
  ".dockerignore"
  ".editorconfig"
  ".gitattributes"
  ".github/workflows/repository-checks.yml"
  ".gitignore"
  ".npmrc"
  "renovate.json"
  "dependency-age-exceptions.json"
  ".node-version"
  ".python-version"
  "package-lock.json"
  "package.json"
  "pyproject.toml"
  "tsconfig.json"
  "uv.lock"
  "packages/backend-role/src/llmrouter_backend/database/migrations/0001_control_foundation.up.sql"
  "packages/backend-role/src/llmrouter_backend/database/migrations/0001_control_foundation.down.sql"
  "packages/backend-role/src/llmrouter_backend/database/migrations/0002_runtime_ledger.up.sql"
  "packages/backend-role/src/llmrouter_backend/database/migrations/0002_runtime_ledger.down.sql"
  "scripts/agent-next-task.sh"
  "scripts/check-database.sh"
  "scripts/check-client-packages.sh"
  "scripts/check-contract-models.sh"
  "scripts/generate-contract-models.py"
)

for required_file in "${required_files[@]}"; do
  if [[ ! -f "${repository_root}/${required_file}" ]]; then
    echo "Required file is missing: ${required_file}" >&2
    exit 1
  fi
done

uv run --with 'ruamel.yaml==0.18.15' python - "${repository_root}" <<'PY'
import sys
from pathlib import Path

from ruamel.yaml import YAML

repository_root = Path(sys.argv[1])
yaml = YAML(typ="safe")
yaml.allow_duplicate_keys = False

for skill_path in sorted((repository_root / ".claude/skills").glob("*/SKILL.md")):
    text = skill_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip() or not parts[2].strip():
        relative_path = skill_path.relative_to(repository_root)
        raise SystemExit(f"Invalid skill front matter: {relative_path}")

    metadata = yaml.load(parts[1])
    expected_name = skill_path.parent.name
    if not isinstance(metadata, dict) or metadata.get("name") != expected_name:
        relative_path = skill_path.relative_to(repository_root)
        raise SystemExit(f"Invalid skill name: {relative_path}")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        relative_path = skill_path.relative_to(repository_root)
        raise SystemExit(f"Invalid skill description: {relative_path}")

    agent_path = skill_path.parent / "agents/openai.yaml"
    if agent_path.is_file():
        agent = yaml.load(agent_path.read_text(encoding="utf-8"))
        interface = agent.get("interface") if isinstance(agent, dict) else None
        interface_keys = ("display_name", "short_description", "default_prompt")
        if not isinstance(interface, dict) or any(
            not isinstance(interface.get(key), str) or not interface[key].strip()
            for key in interface_keys
        ):
            relative_path = agent_path.relative_to(repository_root)
            raise SystemExit(f"Invalid skill agent metadata: {relative_path}")
PY

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
grep -qx 'openapi: 3.1.0' "${repository_root}/docs/api/openapi.yaml"
grep -qx '  version: 1.0.0' "${repository_root}/docs/api/openapi.yaml"
"${repository_root}/scripts/check-api-contracts.sh"
"${repository_root}/scripts/check-dependency-policy.sh"

if [[ "${LLMROUTER_FULL_CHECKS:-0}" == "1" ]]; then
  "${repository_root}/scripts/check-python.sh"
  "${repository_root}/scripts/check-node.sh"
  "${repository_root}/scripts/check-database.sh"
fi

if command -v bd >/dev/null 2>&1; then
  bd -C "${repository_root}" lint --status all >/dev/null
  bd -C "${repository_root}" dep cycles >/dev/null
  bd -C "${repository_root}" graph check >/dev/null
  beads_items="$(bd -C "${repository_root}" list --all --json --limit 0)"
  jq -e 'type == "array"' <<<"${beads_items}" >/dev/null
  "${repository_root}/scripts/check-beads-plan.sh"
fi

git -C "${repository_root}" diff --check
git -C "${repository_root}" diff --cached --check
grep -Fqx "Copyright 2026 tubededentifrice" "${repository_root}/LICENSE.md"
grep -Fq "FSL-1.1-ALv2" "${repository_root}/README.md"

echo "Repository checks passed."

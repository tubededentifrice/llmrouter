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
  "docs/api/contract-policy.yaml"
  "docs/api/contract-digests.json"
  "docs/api/errors.md"
  "docs/api/openapi.yaml"
  "docs/api/stream-protocol.md"
  "docs/api/fixtures/assignment.json"
  "docs/api/fixtures/embedding-result.json"
  "docs/api/fixtures/health.json"
  "docs/api/fixtures/media-job.json"
  "docs/api/fixtures/model-call-result.json"
  "docs/api/fixtures/provider-model.json"
  "docs/api/fixtures/request-log.json"
  "docs/api/fixtures/statistics.json"
  "docs/api/fixtures/workspace.json"
  "docs/decisions/README.md"
  "docs/decisions/0001-reset-to-the-simplified-calling-service.md"
  "docs/decisions/0002-use-fsl-1-1-with-an-apache-2-0-future-license.md"
  "docs/decisions/0003-keep-calling-service-work-in-its-repository.md"
  "docs/decisions/0004-use-the-python-postgresql-fastapi-react-and-vite-foundation.md"
  "docs/decisions/0005-use-built-in-encrypted-provider-credential-storage.md"
  "docs/decisions/0006-use-structured-control-fields-and-standard-endpoint-trust.md"
  "docs/decisions/0007-keep-the-applied-npm-security-pins.md"
  "docs/interviews/service-simplification-2026-08-23.md"
  "docs/research/README.md"
  "docs/research/source-services-2026-08.md"
  "docs/specs/README.md"
  "docs/specs/00-product-scope-and-ownership.md"
  "docs/specs/01-services-workspaces-and-assignments.md"
  "docs/specs/02-providers-models-prices-and-configuration.md"
  "docs/specs/03-model-embedding-and-media-calls.md"
  "docs/specs/04-authentication-administration-and-shared-ui.md"
  "docs/specs/05-accounting-logs-retention-and-operations.md"
  "docs/specs/06-python-sdk-and-shared-harness.md"
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
  "scripts/check-contract-digests.sh"
  "scripts/generate-contract-digests.py"
  "scripts/check-local-development.py"
  "scripts/local-development-bootstrap.py"
  "scripts/local-development-migrate.py"
  "scripts/local-development.sh"
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
uv run python "${repository_root}/scripts/check-local-development.py"

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

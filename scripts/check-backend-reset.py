"""Reject backend surfaces removed by the simplification reset."""
# ruff: noqa: EM102, INP001, TRY003

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "packages/backend-role/src/llmrouter_backend"
REQUIRED_FOUNDATION_FILES = {
    "__init__.py",
    "app.py",
    "database/__init__.py",
    "database/migrations/__init__.py",
    "database/migrations/0001_foundation.up.sql",
    "database/migrations/0001_foundation.down.sql",
}
REMOVED_PATHS = {
    "packages/backend-role/src/llmrouter_backend/admission",
    "packages/backend-role/src/llmrouter_backend/agent_runs",
    "packages/backend-role/src/llmrouter_backend/budgets",
    "packages/backend-role/src/llmrouter_backend/embed_sessions",
    "packages/backend-role/src/llmrouter_backend/local_runtime.py",
    "packages/backend-role/src/llmrouter_backend/request_recovery",
    "packages/backend-role/src/llmrouter_backend/runtime_roles",
    "packages/backend-role/src/llmrouter_backend/spool",
    "packages/backend-role/src/llmrouter_backend/token_exchange",
    "packages/backend-role/src/llmrouter_backend/tool_gateway",
    "scripts/administrator-grant.py",
    "scripts/local-development-bootstrap.py",
    "scripts/local-development-e2e.py",
    "scripts/local-development-live-openrouter.py",
}
REMOVED_MIGRATION_NAMES = re.compile(
    r"(?:control_foundation|runtime_ledger|machine_credentials|"
    r"configuration_publication|admission_binding|budget_reservations|"
    r"budget_allowances|execution_lifecycle|embed_sessions|"
    r"routing_success_commit_boundary)"
)
ACTIVE_REFERENCE_PATHS = {
    "apps/admin/src",
    "apps/admin/vite.config.ts",
    "docker-compose.dev.yml",
    "package.json",
    "packages/backend-role/pyproject.toml",
    "packages/backend-role/src/llmrouter_backend",
    "scripts/local-development-migrate.py",
    "scripts/local_development_admin_session.py",
    "scripts/local-development.sh",
}
FORBIDDEN_REFERENCE = re.compile(
    r"(?:control[-_ ]plane|data[-_ ]plane|token[-_ ]worker|"
    r"PostgresAdmissionRepository|BudgetAllowance|LocalCanonicalSpool|"
    r"agent[-_ ]runs?|EmbedSession|ConfigurationRevision|"
    r"configuration[-_ ]publication|request[-_ ]recovery|runtime[-_ ]roles?|"
    r"token[-_ ]exchange|tool[-_ ]gateway|frame[-_ ]origin|"
    r"hosted[-_ ]service|administrator[-_ ]grant|service[-_ ]grant|"
    r"openai[-_ ]caller|"
    r"@application\.(?:post|get)\(\s*[\"']/v1/(?:chat/completions|responses))",
    re.IGNORECASE,
)


def _active_files(path: Path) -> tuple[Path, ...]:
    """Return implementation files without generated Python caches."""
    if path.is_file():
        return (path,)
    return tuple(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and "__pycache__" not in candidate.parts
    )


def main() -> None:
    """Check the clean base without blocking accepted future features."""
    actual = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in _active_files(PACKAGE_ROOT)
    }
    missing = sorted(REQUIRED_FOUNDATION_FILES - actual)
    if missing:
        raise SystemExit(f"A backend foundation file is missing: {missing[0]}")

    remaining = sorted(
        path for path in REMOVED_PATHS if _active_files(REPOSITORY_ROOT / path)
    )
    if remaining:
        raise SystemExit(f"An obsolete backend path remains: {remaining[0]}")
    migrations = PACKAGE_ROOT / "database/migrations"
    for path in _active_files(migrations):
        if REMOVED_MIGRATION_NAMES.search(path.name) is not None:
            raise SystemExit(f"An obsolete migration remains: {path.name}")

    for relative_path in sorted(ACTIVE_REFERENCE_PATHS):
        for path in _active_files(REPOSITORY_ROOT / relative_path):
            match = FORBIDDEN_REFERENCE.search(path.read_text(encoding="utf-8"))
            if match is not None:
                shown_path = path.relative_to(REPOSITORY_ROOT)
                raise SystemExit(
                    f"An active foundation file names a removed backend surface: "
                    f"{shown_path}: {match.group(0)}"
                )
    print("Backend simplification reset checks passed.")


if __name__ == "__main__":
    main()

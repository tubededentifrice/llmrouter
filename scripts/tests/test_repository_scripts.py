"""Regression checks for clean repository test commands."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_database_check_installs_all_workspace_packages() -> None:
    """Load backend test dependencies in a clean root uv environment."""
    script = (REPOSITORY_ROOT / "scripts/check-database.sh").read_text(encoding="utf-8")
    assert "uv run --all-packages pytest packages/backend-role/tests/database" in script


def test_node_check_installs_the_locked_dependency_tree() -> None:
    """Install exact Node dependencies before repository tools run."""
    script = (REPOSITORY_ROOT / "scripts/check-node.sh").read_text(encoding="utf-8")
    assert script.index("npm ci") < script.index("npm run format:check")


def test_bandit_suppressions_are_narrow_and_scanned() -> None:
    """Keep the fixed public values under the normal Bandit source scan."""
    script = (REPOSITORY_ROOT / "scripts/check-python.sh").read_text(encoding="utf-8")
    errors = (
        REPOSITORY_ROOT
        / "packages/backend-role/src/llmrouter_backend/authority/errors.py"
    ).read_text(encoding="utf-8")
    authority_testing = (
        REPOSITORY_ROOT
        / "packages/backend-role/src/llmrouter_backend/testing/authority.py"
    ).read_text(encoding="utf-8")
    assert "packages/backend-role/src" in script
    assert "\n  -s " not in script
    assert "--skip B105" not in script
    assert "--skip B106" not in script
    assert 'INVALID_TOKEN = "invalid_token"  # noqa: S105  # nosec B105' in errors
    assert 'token_id="test-token",  # noqa: S106  # nosec B106' in authority_testing

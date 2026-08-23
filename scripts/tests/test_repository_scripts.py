"""Regression checks for repository quality commands."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_RESET_PATH = REPOSITORY_ROOT / "scripts/check-backend-reset.py"
DEPENDENCY_POLICY_PATH = REPOSITORY_ROOT / "scripts/check-dependency-policy.py"


def _backend_reset_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_backend_reset", BACKEND_RESET_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _dependency_policy_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_dependency_policy", DEPENDENCY_POLICY_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_dependency_policy_accepts_approved_python_security_update() -> None:
    """Accept only the mature exact cryptography security update."""
    module = _dependency_policy_module()
    policy = {"exclude-newer-package": {"cryptography": "2026-08-09T00:00:00Z"}}
    assert not module.check_python_security_overrides(
        policy,
        ["cryptography==50.0.0"],
        now=module.datetime(2026, 8, 23, tzinfo=module.UTC),
    )


@pytest.mark.parametrize(
    ("policy", "dependencies", "message"),
    [
        (
            {"exclude-newer-package": {"unknown": "2026-08-09T00:00:00Z"}},
            ["cryptography==50.0.0"],
            "approved security update cutoffs",
        ),
        (
            {"exclude-newer-package": {"cryptography": "2026-08-09T00:00:00Z"}},
            ["cryptography==49.0.0"],
            "approved security version 50.0.0",
        ),
        (
            {"exclude-newer-package": "cryptography=2026-08-09"},
            ["cryptography==50.0.0"],
            "approved security update cutoffs",
        ),
    ],
)
def test_dependency_policy_rejects_unsafe_python_override(
    policy: dict[str, object], dependencies: list[str], message: str
) -> None:
    """Reject an unknown, unsupported, or malformed Python override."""
    module = _dependency_policy_module()
    errors = module.check_python_security_overrides(
        policy,
        dependencies,
        now=module.datetime(2026, 8, 23, tzinfo=module.UTC),
    )
    assert any(message in error for error in errors)


def test_dependency_policy_rejects_future_python_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an approved cutoff that is not 14 complete days old."""
    module = _dependency_policy_module()
    monkeypatch.setitem(
        module.APPROVED_PYTHON_OVERRIDES["cryptography"],
        "exclude_newer",
        "2026-08-22T00:00:00Z",
    )
    policy = {"exclude-newer-package": {"cryptography": "2026-08-22T00:00:00Z"}}
    errors = module.check_python_security_overrides(
        policy,
        ["cryptography==50.0.0"],
        now=module.datetime(2026, 8, 23, tzinfo=module.UTC),
    )
    assert any("cutoff is less than 14 days old" in error for error in errors)


def test_database_check_runs_the_clean_migration_suite() -> None:
    """Load backend dependencies before the PostgreSQL tests run."""
    script = (REPOSITORY_ROOT / "scripts/check-database.sh").read_text(encoding="utf-8")
    assert "uv run --all-packages pytest packages/backend-role/tests/database" in script
    assert "postgres_ready=0" in script
    assert 'if [[ "${postgres_ready}" -ne 1 ]]; then' in script


def test_backend_reset_gate_accepts_the_clean_foundation() -> None:
    """Accept the clean base without blocking later accepted work."""
    _backend_reset_module().main()


def test_backend_reset_gate_rejects_a_removed_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail when an obsolete backend script path remains."""
    module = _backend_reset_module()
    monkeypatch.setattr(module, "REMOVED_PATHS", {"README.md"})
    with pytest.raises(SystemExit, match="obsolete backend path"):
        module.main()


def test_backend_reset_gate_rejects_a_removed_runtime_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail when an active source restores a removed runtime protocol."""
    module = _backend_reset_module()
    probe = tmp_path / "probe.py"
    probe.write_text("request_recovery = True\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "REMOVED_PATHS", set())
    monkeypatch.setattr(module, "ACTIVE_REFERENCE_PATHS", {"probe.py"})
    with pytest.raises(SystemExit, match="removed backend surface"):
        module.main()


def test_node_check_installs_the_locked_dependency_tree() -> None:
    """Install exact Node dependencies before repository tools run."""
    script = (REPOSITORY_ROOT / "scripts/check-node.sh").read_text(encoding="utf-8")
    assert script.index("npm ci") < script.index("npm run format:check")


def test_python_check_scans_the_complete_backend_source() -> None:
    """Keep strict checks and Bandit on the replacement application."""
    script = (REPOSITORY_ROOT / "scripts/check-python.sh").read_text(encoding="utf-8")
    assert "packages/backend-role/src" in script
    assert "uv run mypy packages" in script
    assert "uv run bandit -q -r" in script

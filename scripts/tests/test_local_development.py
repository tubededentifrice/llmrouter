"""Regression tests for the localhost development deployment."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = REPOSITORY_ROOT / "scripts/check-local-development.py"
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.dev.yml"
BOOTSTRAP_PATH = REPOSITORY_ROOT / "scripts/local-development-bootstrap.py"


def _check_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_local_development", CHECK_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _bootstrap_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "local_development_bootstrap", BOOTSTRAP_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_local_development_contract_accepts_repository_compose() -> None:
    """Accept the complete immutable loopback deployment."""
    _check_module().main()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("127.0.0.1:8010:8000", "8010:8000", "public port binding"),
        (
            '"127.0.0.1:8010:8000"',
            "0.0.0.0:8010:8000",
            "public port binding",
        ),
        (
            "node:24.17.0-alpine@sha256:156b55f92e98ccd5ef49578a8cea0df4679826564bad1c9d4ef04462b9f0ded6",
            "node:24.17.0-alpine",
            "not immutable",
        ),
    ],
)
def test_local_development_contract_rejects_unsafe_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    """Reject a public port or floating image before local startup."""
    unsafe = tmp_path / "docker-compose.dev.yml"
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    assert old in source
    unsafe.write_text(source.replace(old, new, 1), encoding="utf-8")
    module = _check_module()
    monkeypatch.setattr(module, "COMPOSE_PATH", unsafe)
    with pytest.raises(SystemExit, match=message):
        module.main()


def test_local_start_script_rejects_a_public_address() -> None:
    """Keep the wrapper public-binding failure explicit and early."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    validation = script.index("validate_environment")
    startup = script.index("compose up")
    assert validation < startup
    assert "LLMROUTER_BIND_ADDRESS:-127.0.0.1" in script
    assert "can bind only to 127.0.0.1" in script


def test_local_start_serializes_operations_and_installs_secrets_exclusively() -> None:
    """Prevent concurrent startup and unsafe secret replacement."""
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert "flock --nonblock" in script
    assert 'ln "${temporary}" "${target}"' in script
    assert "stat -c %h" in script
    assert ">${target}" not in script
    assert "cleanup_failed_start" in script


def test_bootstrap_writer_rejects_a_secret_symlink(tmp_path: Path) -> None:
    """Do not follow a replaced local secret path."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="ascii")
    target = tmp_path / "token"
    target.symlink_to(victim)
    with pytest.raises(SystemExit, match="unsafe"):
        _bootstrap_module()._write_secret(target, "replacement")  # noqa: SLF001
    assert victim.read_text(encoding="ascii") == "keep\n"


def test_bootstrap_writer_rejects_a_secret_hard_link(tmp_path: Path) -> None:
    """Do not truncate a multiply linked secret file."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="ascii")
    target = tmp_path / "token"
    target.hardlink_to(victim)
    with pytest.raises(SystemExit, match="unsafe"):
        _bootstrap_module()._write_secret(target, "replacement")  # noqa: SLF001
    assert victim.read_text(encoding="ascii") == "keep\n"


def test_local_secret_paths_are_ignored_and_not_printed() -> None:
    """Keep generated secret material outside tracked and displayed output."""
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    script = (REPOSITORY_ROOT / "scripts/local-development.sh").read_text(
        encoding="utf-8"
    )
    assert ".local-development/" in ignore
    assert "set -x" not in script
    assert "cat /run/secrets" not in script
    assert "LLMROUTER_OPENROUTER_API_KEY" not in script

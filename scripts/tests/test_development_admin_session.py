"""Tests for the short-lived localhost administrator test session."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/local_development_admin_session.py"
TOKEN = "A" * 43
CSRF = "E" * 43
SESSION_FILE_MODE = 0o600


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "local_development_admin_session_test", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _private_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state)
    return module


def _document(*, expires_at: datetime | None = None) -> dict[str, str]:
    return {
        "cookie_name": "llmrouter_admin_session",
        "cookie_value": TOKEN,
        "csrf_token": CSRF,
        "expires_at": (
            expires_at or datetime.now(tz=UTC) + timedelta(minutes=10)
        ).isoformat(),
        "origin": "http://127.0.0.1:5174",
    }


def _install(module: ModuleType, document: dict[str, str]) -> Path:
    temporary = module._write_temporary_document(document)  # noqa: SLF001
    module._install_temporary_document(temporary, None)  # noqa: SLF001
    return cast("Path", module.STATE_DIRECTORY / module.SESSION_FILE_NAME)


def test_private_session_file_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read one bounded mode-0600 session without changing its controls."""
    module = _private_state(monkeypatch, tmp_path)
    path = _install(module, _document())
    session = module.read_development_administrator_session()
    assert session.cookie_value == TOKEN
    assert session.csrf_token == CSRF
    assert stat.S_IMODE(path.stat().st_mode) == SESSION_FILE_MODE


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_session_file_rejects_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe_kind: str
) -> None:
    """Reject a linked session control file."""
    module = _private_state(monkeypatch, tmp_path)
    state = module.STATE_DIRECTORY
    source = state / "source"
    source.write_text(json.dumps(_document()), encoding="utf-8")
    source.chmod(0o600)
    target = state / module.SESSION_FILE_NAME
    if unsafe_kind == "symlink":
        target.symlink_to(source)
        expected: type[Exception] = OSError
    else:
        os.link(source, target)
        expected = ValueError
    with pytest.raises(expected):
        module.read_development_administrator_session()


@pytest.mark.parametrize(
    "document",
    [
        {},
        _document(expires_at=datetime.now(tz=UTC) - timedelta(seconds=1)),
        {**_document(), "cookie_value": "short"},
        {**_document(), "csrf_token": "!" * 43},
        {**_document(), "origin": "https://llmrouter.opendle.dev"},
    ],
)
def test_session_file_rejects_invalid_or_expired_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    document: dict[str, str],
) -> None:
    """Fail closed for a malformed, foreign, or expired test session."""
    module = _private_state(monkeypatch, tmp_path)
    _install(module, document)
    with pytest.raises(ValueError, match="session file is invalid"):
        module.read_development_administrator_session()


def test_session_file_rejects_public_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject a state directory that another account can read."""
    module = _private_state(monkeypatch, tmp_path)
    module.STATE_DIRECTORY.chmod(0o755)
    with pytest.raises(ValueError, match="state directory is unsafe"):
        module.read_development_administrator_session()


def test_session_file_rejects_duplicate_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject a duplicate security field in the private JSON document."""
    module = _private_state(monkeypatch, tmp_path)
    path = module.STATE_DIRECTORY / module.SESSION_FILE_NAME
    path.write_text(
        '{"cookie_name":"llmrouter_admin_session",'
        f'"cookie_value":"{TOKEN}","cookie_value":"{CSRF}",'
        f'"csrf_token":"{CSRF}",'
        f'"expires_at":"{(datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()}",'
        '"origin":"http://127.0.0.1:5174"}',
        encoding="utf-8",
    )
    path.chmod(SESSION_FILE_MODE)
    with pytest.raises(ValueError, match="session file is invalid"):
        module.read_development_administrator_session()


def test_script_rejects_unknown_action_without_a_control_value() -> None:
    """Reject unsafe command input before a session or file operation."""
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(  # noqa: S603 - Fixed local script path.
        [uv, "run", "python", SCRIPT, "unknown"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "{create|clear}" in result.stderr
    assert TOKEN not in result.stdout + result.stderr
    assert CSRF not in result.stdout + result.stderr

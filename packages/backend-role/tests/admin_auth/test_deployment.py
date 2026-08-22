"""Focused fail-closed public administrator deployment tests."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from llmrouter_backend.admin_auth.deployment import configured_repository

if TYPE_CHECKING:
    from pathlib import Path


def test_public_authentication_is_off_only_with_the_exact_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLMROUTER_PUBLIC_ADMIN_AUTH", "0")
    assert configured_repository("postgresql://unused") is None
    monkeypatch.setenv("LLMROUTER_PUBLIC_ADMIN_AUTH", "true")
    with pytest.raises(RuntimeError, match="flag is invalid"):
        configured_repository("postgresql://unused")


def test_enabled_public_authentication_rejects_empty_secret_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LLMROUTER_PUBLIC_ADMIN_AUTH", "1")
    names = (
        "LLMROUTER_OIDC_CLIENT_ID_FILE",
        "LLMROUTER_OIDC_CLIENT_SECRET_FILE",
        "LLMROUTER_ADMIN_DIGEST_KEY_FILE",
        "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE",
    )
    for name in names:
        path = tmp_path / name.lower()
        path.write_text("")
        monkeypatch.setenv(name, str(path))
    with pytest.raises(RuntimeError, match="invalid"):
        configured_repository("postgresql://unused")


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        ("client_id", "client id\n"),
        ("client_secret", "A" * 31 + "\n"),
        ("digest_key", "YQ==\n"),
        ("encryption_key", " " + "YQ"),
    ],
)
def test_public_authentication_rejects_unsafe_secret_formats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    payload: str,
) -> None:
    monkeypatch.setenv("LLMROUTER_PUBLIC_ADMIN_AUTH", "1")
    values = {
        "client_id": "router-client",
        "client_secret": "A" * 32,
        "digest_key": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE",
        "encryption_key": "YmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI",
    }
    values[target] = payload
    names = {
        "client_id": "LLMROUTER_OIDC_CLIENT_ID_FILE",
        "client_secret": "LLMROUTER_OIDC_CLIENT_SECRET_FILE",
        "digest_key": "LLMROUTER_ADMIN_DIGEST_KEY_FILE",
        "encryption_key": "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE",
    }
    for key, name in names.items():
        path = tmp_path / key
        path.write_text(values[key])
        monkeypatch.setenv(name, str(path))
    with pytest.raises(RuntimeError, match=r"invalid|unavailable"):
        configured_repository("postgresql://unused")

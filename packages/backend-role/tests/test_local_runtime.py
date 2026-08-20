"""Local complete-runtime security and replay tests."""
# ruff: noqa: D103, PLR2004

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from llmrouter_backend.local_runtime import (
    LOCAL_ADMIN_ORIGIN,
    LOCAL_SOURCE_NODE_ID,
    LocalAdministratorAuthority,
    LocalReplayProtector,
    _local_openrouter,
    _router,
)
from llmrouter_backend.spool import CanonicalEvent, EventClass

if TYPE_CHECKING:
    from pathlib import Path

EVENT_ID = "0198a080-0000-7000-8000-000000000151"


def _event(payload: bytes = b'{"usage":"bounded"}') -> CanonicalEvent:
    return CanonicalEvent(
        EVENT_ID,
        LOCAL_SOURCE_NODE_ID,
        1,
        EventClass.ACCOUNTING,
        payload,
        datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_local_replay_evidence_is_encrypted_durable_and_conflict_safe(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "accounting-replay.bin"
    event = _event()
    protector = LocalReplayProtector(path, b"r" * 32)
    position = protector.protect(event, hashlib.sha256(event.payload).digest())
    assert position == f"local-replay:{EVENT_ID}"
    assert protector.protect(event, hashlib.sha256(event.payload).digest()) == position
    changed = _event(b'{"usage":"changed"}')
    with pytest.raises(RuntimeError, match="conflicts"):
        protector.protect(changed, hashlib.sha256(changed.payload).digest())
    protector.close()
    assert event.payload not in path.read_bytes()

    recovered = LocalReplayProtector(path, b"r" * 32)
    assert recovered.protect(event, hashlib.sha256(event.payload).digest()) == position
    recovered.close()


def test_local_openrouter_requires_bounded_model_and_authorization() -> None:
    body = json.dumps(
        {
            "model": "deepseek/deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Local proof."}],
            "stream": False,
        }
    ).encode()
    accepted = _local_openrouter(
        httpx.Request(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer generated-test-value"},
            content=body,
        )
    )
    assert accepted.status_code == 200
    assert accepted.json()["choices"][0]["message"]["content"] == "local response"

    denied = _local_openrouter(
        httpx.Request(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            content=body,
        )
    )
    assert denied.status_code == 401


def test_local_administrator_activation_sets_only_a_protected_cookie() -> None:
    app = FastAPI()
    secret = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    app.state.local_admin_authority = LocalAdministratorAuthority(secret, csrf)
    app.include_router(_router)
    client = TestClient(app)

    capability = client.head("/v1/admin/local-session")
    assert capability.status_code == 204
    assert capability.content == b""
    assert capability.headers["cache-control"] == "no-store"
    assert "/v1/admin/local-session" not in app.openapi()["paths"]

    response = client.post(
        "/v1/admin/local-session",
        headers={"Origin": LOCAL_ADMIN_ORIGIN},
        json={"secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "csrf_token": csrf}
    assert secret not in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-llmrouter-admin=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        ("http://127.0.0.1:5999", 403),
        (LOCAL_ADMIN_ORIGIN, 401),
    ],
)
def test_local_administrator_activation_fails_safely(origin: str, status: int) -> None:
    app = FastAPI()
    expected = secrets.token_urlsafe(32)
    supplied = secrets.token_urlsafe(32)
    app.state.local_admin_authority = LocalAdministratorAuthority(
        expected, secrets.token_urlsafe(32)
    )
    app.include_router(_router)

    response = TestClient(app).post(
        "/v1/admin/local-session",
        headers={"Origin": origin},
        json={"secret": supplied},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == ("local_administrator_activation_failed")
    assert expected not in response.text
    assert supplied not in response.text
    assert "set-cookie" not in response.headers

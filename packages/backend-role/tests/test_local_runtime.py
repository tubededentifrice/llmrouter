"""Local complete-runtime security and replay tests."""
# ruff: noqa: D103, EM101, PLR2004, TRY003

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from llmrouter_backend.accounting import AttemptOutcome as AccountingOutcome
from llmrouter_backend.administration.http import _session
from llmrouter_backend.execution import AdapterStopEvidence
from llmrouter_backend.local_runtime import (
    LOCAL_ADMIN_ORIGIN,
    LOCAL_SOURCE_NODE_ID,
    DualAdministratorAuthority,
    LocalAdministratorAuthority,
    LocalBudgetGate,
    LocalCancelableAdapter,
    LocalReplayProtector,
    _accounting_outcome,
    _local_openrouter,
    _local_openrouter_transport,
    _router,
)
from llmrouter_backend.routing import AttemptOutcome as RoutingOutcome
from llmrouter_backend.spool import CanonicalEvent, EventClass
from starlette.requests import Request

if TYPE_CHECKING:
    from pathlib import Path

EVENT_ID = "0198a080-0000-7000-8000-000000000151"
RESERVATION_ID = "0198a080-0000-7000-8000-000000000152"
ATTEMPT_ID = "0198a080-0000-7000-8000-000000000153"
REQUEST_ROW_ID = "0198a080-0000-7000-8000-000000000154"


def test_local_openrouter_live_transport_needs_the_exact_guard() -> None:
    """Keep deterministic local runs offline unless the exact flag is present."""
    assert isinstance(_local_openrouter_transport(None), httpx.MockTransport)
    assert isinstance(_local_openrouter_transport("0"), httpx.MockTransport)
    assert _local_openrouter_transport("1") is None
    with pytest.raises(RuntimeError, match="flag is invalid"):
        _local_openrouter_transport("true")


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
    assert accepted.json()["usage"]["prompt_tokens_details"] == {"cached_tokens": 0}

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
    client = TestClient(app, base_url=LOCAL_ADMIN_ORIGIN)

    capability = client.head("/v1/admin/local-session")
    assert capability.status_code == 204
    assert capability.content == b""
    assert capability.headers["cache-control"] == "no-store"
    assert "/v1/admin/local-session" not in app.openapi()["paths"]
    public_capability = TestClient(app, base_url="https://llmrouter.opendle.dev").head(
        "/v1/admin/local-session"
    )
    assert public_capability.status_code == 404

    response = client.post(
        "/v1/admin/local-session",
        headers={"Origin": LOCAL_ADMIN_ORIGIN},
        json={"secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "csrf_token": csrf}
    assert secret not in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-llmrouter-local-admin=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie


def test_public_session_mode_cannot_use_the_local_activation_token() -> None:
    secret = secrets.token_urlsafe(32)

    class Production:
        def authorize_session(self, token: str, **_kwargs: object) -> None:
            assert token == secret
            raise PermissionError("The production session is invalid.")

    authority = DualAdministratorAuthority(
        LocalAdministratorAuthority(secret, secrets.token_urlsafe(32)),
        cast("Any", Production()),
    )
    with pytest.raises(PermissionError, match="production session"):
        authority.authorize_session(f"oidc:{secret}")
    with pytest.raises(PermissionError, match="mode is invalid"):
        authority.authorize_session(secret)


def test_http_boundary_selects_local_cookie_only_on_the_exact_local_host() -> None:
    secret = secrets.token_urlsafe(32)
    app = FastAPI()
    app.state.dual_administrator_authority = True

    def request(host: str, cookie: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http" if host == "127.0.0.1:5174" else "https",
                "path": "/v1/admin/state",
                "raw_path": b"/v1/admin/state",
                "query_string": b"",
                "headers": [
                    (b"host", host.encode()),
                    (b"cookie", cookie.encode()),
                ],
                "server": (host.partition(":")[0], 5174),
                "client": ("127.0.0.1", 1),
                "app": app,
            }
        )

    local = request("127.0.0.1:5174", f"__Host-llmrouter-local-admin={secret}")
    public = request("llmrouter.opendle.dev", f"__Host-llmrouter-admin={secret}")
    assert _session(local, "request") == f"local:{secret}"
    assert _session(public, "request") == f"oidc:{secret}"


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

    response = TestClient(app, base_url=LOCAL_ADMIN_ORIGIN).post(
        "/v1/admin/local-session",
        headers={"Origin": origin},
        json={"secret": supplied},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == ("local_administrator_activation_failed")
    assert expected not in response.text
    assert supplied not in response.text
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", LOCAL_ADMIN_ORIGIN), ("Origin", LOCAL_ADMIN_ORIGIN)],
        [("Origin", LOCAL_ADMIN_ORIGIN), ("Content-Length", "2048")],
        [("Origin", LOCAL_ADMIN_ORIGIN), ("Content-Type", "text/plain")],
        [("Origin", LOCAL_ADMIN_ORIGIN), ("Transfer-Encoding", "chunked")],
    ],
)
def test_local_administrator_activation_rejects_ambiguous_or_unbounded_input(
    headers: list[tuple[str, str]],
) -> None:
    app = FastAPI()
    expected = secrets.token_urlsafe(32)
    app.state.local_admin_authority = LocalAdministratorAuthority(
        expected, secrets.token_urlsafe(32)
    )
    app.include_router(_router)

    response = TestClient(app, base_url=LOCAL_ADMIN_ORIGIN).post(
        "/v1/admin/local-session",
        headers=headers,
        content=json.dumps({"secret": expected}),
    )

    assert response.status_code in {400, 403}
    assert response.json()["error"]["code"] == ("local_administrator_activation_failed")
    assert expected not in response.text
    assert "set-cookie" not in response.headers


def test_local_adapter_shutdown_stops_each_active_stream() -> None:
    class _Adapter:
        def __init__(self) -> None:
            self.cancelled: list[object] = []

        def cancel(self, plan: object) -> AdapterStopEvidence:
            self.cancelled.append(plan)
            return AdapterStopEvidence(
                "attempt-one",
                supported=True,
                stop_requested=True,
                confirmed_stopped=True,
                safe_code="local-test-stopped",
            )

    underlying = _Adapter()
    adapter = LocalCancelableAdapter(underlying)  # type: ignore[arg-type]
    plan = cast(
        "Any", SimpleNamespace(attempt_id="attempt-one", request_id="request-one")
    )
    adapter._active[plan.attempt_id] = plan  # noqa: SLF001

    adapter.close()

    assert underlying.cancelled == [plan]


def test_local_adapter_confirms_a_requested_active_stop() -> None:
    class _Adapter:
        def cancel(self, plan: object) -> AdapterStopEvidence:
            del plan
            return AdapterStopEvidence(
                "attempt-one",
                supported=True,
                stop_requested=True,
                confirmed_stopped=False,
                safe_code="local-transport-closed",
            )

    adapter = LocalCancelableAdapter(_Adapter())  # type: ignore[arg-type]
    plan = cast(
        "Any", SimpleNamespace(attempt_id="attempt-one", request_id="request-one")
    )
    adapter._active[plan.attempt_id] = plan  # noqa: SLF001

    evidence = adapter.cancel(plan)

    assert evidence.confirmed_stopped is True
    assert evidence.safe_code == "local_deterministic_transport_stopped"


def test_local_budget_retry_evidence_uses_durable_times_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurred_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    class _Rows:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_error: object) -> None:
            return None

        def execute(self, *_args: object) -> _Rows:
            return self

        def fetchone(self) -> tuple[datetime]:
            return (occurred_at,)

    class _Repository:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def reconcile(self, _context: object, _identity: str, **values: object) -> None:
            self.calls.append(values)

    repository = _Repository()
    gate = LocalBudgetGate("postgresql://unused", repository)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "llmrouter_backend.local_runtime.psycopg.connect", lambda *_args: _Rows()
    )

    gate.release(RESERVATION_ID)
    gate.release(RESERVATION_ID)
    plan = cast(
        "Any", SimpleNamespace(attempt_id=ATTEMPT_ID, request_row_id=REQUEST_ROW_ID)
    )

    assert gate.finished_at(plan) == occurred_at
    assert repository.calls[0] == repository.calls[1]
    assert repository.calls[0]["now"] == occurred_at
    assert repository.calls[0]["accounting_event_id"] == str(
        uuid.uuid5(uuid.UUID(RESERVATION_ID), "unused-reservation")
    )


def test_local_cancelled_attempt_keeps_failed_attempt_accounting() -> None:
    assert _accounting_outcome(RoutingOutcome.CANCELLED) is AccountingOutcome.FAILED

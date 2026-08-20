"""Focused service and HTTP tests for embed-session authority."""
# ruff: noqa: ANN001, ANN201, ANN202, ARG002, D102, D107, DTZ001, EM101, PLC0415, PLR0913, PLR2004, S105, S106

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from llmrouter_backend.authority import Audience, ServicePrincipal
from llmrouter_backend.embed_sessions import (
    CreatedSession,
    EmbedSessionError,
    EmbedSessionRequest,
    EmbedSessionService,
    EmbedTheme,
    RedeemedSession,
    install_embed_session_service,
    router,
)
from llmrouter_backend.testing import ScopeTestBuilder
from pydantic import ValidationError

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
SERVICE_ID = "0198a080-0000-7000-8000-000000000001"
WORKSPACE_ID = "0198a080-0000-7000-8000-000000000003"
FRAME_ORIGIN = "https://router.example"
HOST_ORIGIN = "https://host.example"


class FakeAuthenticator:
    """Return one selected principal without accepting another token."""

    def __init__(self, principal: ServicePrincipal) -> None:
        self.principal = principal

    def authenticate(
        self, token: str, *, request_id: str, now: datetime
    ) -> ServicePrincipal:
        if token != "a" * 43:
            from llmrouter_backend.machine_identity import MachineIdentityError

            raise MachineIdentityError("invalid_token", request_id)
        return self.principal


class FakeRepository:
    """Capture exact authorized contexts for service tests."""

    def __init__(self) -> None:
        self.context = None
        self.revoked = None

    def create(self, context, document, *, now):
        self.context = context
        return CreatedSession(
            "0198a080-0000-7000-8000-000000000099",
            "b" * 43,
            f"{FRAME_ORIGIN}/service-administration",
            now + timedelta(minutes=5),
        )

    def redeem(
        self,
        session_id,
        bootstrap_token,
        frame_nonce,
        host_origin,
        *,
        request_origin,
        request_id,
        now,
    ):
        if request_origin != FRAME_ORIGIN or host_origin != HOST_ORIGIN:
            raise EmbedSessionError("not_found", request_id)
        principal = ScopeTestBuilder(
            scope=_workspace_scope(), now=now
        ).embed("configuration.read")
        return RedeemedSession(
            principal=replace(
                principal,
                session_id=session_id,
                host_subject="host-user",
                service_id=SERVICE_ID,
                allowed_workspace_ids=frozenset({WORKSPACE_ID}),
            ),
            session_token="c" * 43,
            theme=EmbedTheme(
                mode="dark", density="compact", corner_style="rounded"
            ),
        )

    def revoke(self, context, session_id, *, now, allowed_workspace_ids=None):
        self.context = context
        self.revoked = (session_id, allowed_workspace_ids)


def _workspace_scope():
    from llmrouter_backend.authority import Scope

    return Scope(SERVICE_ID, WORKSPACE_ID)


def _principal(
    *,
    audience: Audience = Audience.HOST_BACKEND,
    operations: frozenset[str] = frozenset({"admin_embed.create"}),
    workspaces: frozenset[str] | None = None,
    now: datetime = NOW,
) -> ServicePrincipal:
    return ServicePrincipal(
        issuer="test",
        token_id="token",
        audience=audience,
        service_id=SERVICE_ID,
        operations=operations,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=4),
        credential_generation=1,
        allowed_workspace_ids=workspaces,
    )


def _request(*, sensitive: bool = False) -> EmbedSessionRequest:
    permissions = ["configuration.write"] if sensitive else ["configuration.read"]
    return EmbedSessionRequest(
        host_user_subject="host-user",
        workspace_id=WORKSPACE_ID,
        allowed_origin=HOST_ORIGIN,
        permissions=permissions,
        recent_auth_at=NOW - timedelta(minutes=1) if sensitive else None,
        theme=EmbedTheme(mode="dark", density="compact", corner_style="rounded"),
    )


@pytest.mark.parametrize(
    "principal",
    [
        _principal(
            audience=Audience.CONFIGURATION,
            operations=frozenset({"configuration.read"}),
            workspaces=frozenset({WORKSPACE_ID}),
        ),
        replace(
            _principal(workspaces=frozenset({WORKSPACE_ID})),
            service_id="0198a080-0000-7000-8000-000000000002",
        ),
        replace(
            _principal(workspaces=frozenset({WORKSPACE_ID})),
            allowed_workspace_ids=frozenset(),
        ),
    ],
)
def test_create_requires_exact_host_backend_tuple(principal: ServicePrincipal) -> None:
    """Another audience, operation, service, or workspace cannot create authority."""
    repository = FakeRepository()
    service = EmbedSessionService(FakeAuthenticator(principal), repository)
    with pytest.raises(EmbedSessionError):
        service.create("a" * 43, SERVICE_ID, _request(), request_id="request", now=NOW)
    assert repository.context is None


def test_create_and_revoke_use_exact_authorized_context() -> None:
    """The service passes only the closed host-backend context to storage."""
    repository = FakeRepository()
    service = EmbedSessionService(FakeAuthenticator(_principal()), repository)
    created = service.create(
        "a" * 43, SERVICE_ID, _request(), request_id="request-create", now=NOW
    )
    assert created.bootstrap_token == "b" * 43
    assert repository.context.scope == _workspace_scope()
    service.revoke(
        "a" * 43,
        SERVICE_ID,
        created.session_id,
        request_id="request-revoke",
        now=NOW,
    )
    assert repository.revoked == (created.session_id, None)


def test_workspace_token_cannot_create_service_wide_session() -> None:
    """A workspace-limited host token cannot obtain service-wide authority."""
    repository = FakeRepository()
    service = EmbedSessionService(
        FakeAuthenticator(_principal(workspaces=frozenset({WORKSPACE_ID}))),
        repository,
    )
    document = _request().model_copy(update={"workspace_id": None})
    with pytest.raises(EmbedSessionError) as captured:
        service.create(
            "a" * 43, SERVICE_ID, document, request_id="request", now=NOW
        )
    assert captured.value.code == "insufficient_scope"
    assert repository.context is None


def test_closed_request_validation_rejects_escalation_and_bad_origins() -> None:
    """Unknown fields, duplicate permissions, and unsafe origins fail closed."""
    base = _request().model_dump(mode="json")
    for changed in (
        {**base, "permissions": ["configuration.read", "configuration.read"]},
        {**base, "permissions": ["accounting.read"], "workspace_id": None},
        {**base, "allowed_origin": "http://public.example"},
        {**base, "allowed_origin": "https://host.example/path"},
        {**base, "unknown": True},
    ):
        with pytest.raises(ValidationError):
            EmbedSessionRequest.model_validate(changed)


def test_sensitive_request_needs_aware_recent_authentication() -> None:
    """A write permission can reach the semantic missing-authentication check."""
    missing = EmbedSessionRequest(
        **{
            **_request().model_dump(),
            "permissions": ["configuration.write"],
            "recent_auth_at": None,
        }
    )
    assert missing.recent_auth_at is None
    with pytest.raises(ValidationError):
        EmbedSessionRequest(
            **{
                **_request().model_dump(),
                "permissions": ["configuration.write"],
                "recent_auth_at": datetime(2026, 8, 20, 12),
            }
        )


def test_http_routes_set_safe_cookie_and_hide_secrets() -> None:
    """Create and bootstrap are wired with no token in a URL or response body."""
    repository = FakeRepository()
    service = EmbedSessionService(
        FakeAuthenticator(_principal(now=datetime.now(UTC))), repository
    )
    app = FastAPI()
    app.include_router(router)
    install_embed_session_service(app, service)
    client = TestClient(app)
    create = client.post(
        f"/v1/services/{SERVICE_ID}/administration/embed-sessions",
        headers={"Authorization": f"Bearer {'a' * 43}"},
        json=_request().model_dump(mode="json"),
    )
    assert create.status_code == 201
    assert "bootstrap_token" in create.json()
    assert "bootstrap_token" not in create.json()["frame_url"]
    bootstrap = client.post(
        f"/v1/administration/embed-sessions/{create.json()['session_id']}/bootstrap",
        headers={"Origin": FRAME_ORIGIN},
        json={
            "bootstrap_token": "b" * 43,
            "frame_nonce": "nonce-0123456789",
            "host_origin": HOST_ORIGIN,
        },
    )
    assert bootstrap.status_code == 200
    assert "token" not in bootstrap.text
    cookie = bootstrap.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=none" in cookie
    assert "Domain=" not in cookie


def test_http_errors_do_not_echo_bootstrap_input() -> None:
    """A wrong origin returns one safe error without the submitted secret."""
    service = EmbedSessionService(FakeAuthenticator(_principal()), FakeRepository())
    app = FastAPI()
    app.include_router(router)
    install_embed_session_service(app, service)
    secret = "wrong-secret-value-that-is-long-enough-000000"
    response = TestClient(app).post(
        "/v1/administration/embed-sessions/opaque/bootstrap",
        headers={"Origin": "https://wrong.example"},
        json={
            "bootstrap_token": secret,
            "frame_nonce": "nonce-0123456789",
            "host_origin": HOST_ORIGIN,
        },
    )
    assert response.status_code == 404
    assert secret not in response.text

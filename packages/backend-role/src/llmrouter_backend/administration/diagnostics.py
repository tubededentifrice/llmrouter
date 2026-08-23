"""Run one bounded administrator diagnostic through normal model routing."""
# ruff: noqa: D102, D107, EM101, TRY003

from __future__ import annotations

import json
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from llmrouter_backend.authority import (
    RECENT_AUTH_LIMIT,
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    ServicePrincipal,
)

if TYPE_CHECKING:
    from llmrouter_backend.model_requests import ModelRequestService
    from llmrouter_backend.routing import DiagnosticGrant


class MachineAuthenticator(Protocol):
    """Authenticate the normal machine token path."""

    def authenticate(
        self, token: str, *, request_id: str, now: datetime
    ) -> ServicePrincipal: ...


class DiagnosticGrantStore(Protocol):
    """Create the existing exact, single-use route grant."""

    def create_diagnostic_grant(
        self,
        context: RequestContext,
        *,
        exact_route_id: str,
        reason: str,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> DiagnosticGrant: ...


class TransientDiagnosticAuthenticator:
    """Add one-use in-process model authority to the normal authenticator."""

    def __init__(self, fallback: MachineAuthenticator) -> None:
        self._fallback = fallback
        self._lock = threading.Lock()
        self._principals: dict[str, ServicePrincipal] = {}

    def issue(self, context: RequestContext, *, now: datetime) -> str:
        """Issue one exact short-lived internal token after administrator checks."""
        recent = context.recent_authentication_at
        if (
            context.operation != "diagnostic.run"
            or not context.mutation
            or context.actor_kind is not PrincipalKind.ADMINISTRATOR
            or context.authority_path is not AuthorityPath.GLOBAL_ADMINISTRATION
            or context.authority_class is not AuthorityClass.GLOBAL_ADMINISTRATOR
            or context.scope.service_id is None
            or recent is None
            or recent > now
            or now - recent > RECENT_AUTH_LIMIT
        ):
            raise PermissionError("The diagnostic administrator authority is invalid.")
        token = secrets.token_urlsafe(32)
        allowed_workspaces = (
            frozenset()
            if context.scope.workspace_id is None
            else frozenset({context.scope.workspace_id})
        )
        principal = ServicePrincipal(
            issuer="llmrouter-administrator-diagnostic",
            token_id=str(uuid.uuid4()),
            audience=Audience.DATA_PLANE,
            service_id=context.scope.service_id,
            operations=frozenset({"model.create"}),
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            credential_generation=1,
            allowed_workspace_ids=allowed_workspaces,
        )
        with self._lock:
            self._principals[token] = principal
        return token

    def discard(self, token: str) -> None:
        """Remove an internal token after an interrupted create operation."""
        with self._lock:
            self._principals.pop(token, None)

    def authenticate(
        self, token: str, *, request_id: str, now: datetime
    ) -> ServicePrincipal:
        """Consume one internal token or use the normal machine authenticator."""
        with self._lock:
            principal = self._principals.pop(token, None)
        if principal is not None:
            return principal
        return self._fallback.authenticate(token, request_id=request_id, now=now)


class AdministratorDiagnosticRunner:
    """Create and consume one exact grant through the normal request pipeline."""

    def __init__(
        self,
        grants: DiagnosticGrantStore,
        models: ModelRequestService,
        authenticator: TransientDiagnosticAuthenticator,
    ) -> None:
        self._grants = grants
        self._models = models
        self._authenticator = authenticator

    def run(
        self,
        context: RequestContext,
        *,
        logical_request_id: str,
        exact_route_id: str,
        reason: str,
        now: datetime,
    ) -> dict[str, object]:
        """Admit one fixed content-safe probe and return no prompt or output."""
        grant = self._grants.create_diagnostic_grant(
            context,
            exact_route_id=exact_route_id,
            reason=reason,
            now=now,
        )
        token = self._authenticator.issue(context, now=now)
        body: dict[str, object] = {
            "api_version": "1",
            "data_profile": "service-data",
            "exact_route": grant.exact_route_id,
            "exact_route_grant": grant.grant,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
            "limits": {"attempt_timeout_ms": 30_000, "max_output_units": 16},
            "output": {"format": "text", "temperature": 0},
        }
        if grant.workspace_id is not None:
            body["workspace_id"] = grant.workspace_id
        try:
            result = self._models.create(
                token,
                logical_request_id,
                json.dumps(body, separators=(",", ":")).encode(),
                error_request_id=context.request_id,
            )
        finally:
            self._authenticator.discard(token)
        return {
            "request_id": logical_request_id,
            "service_id": grant.service_id,
            "workspace_id": grant.workspace_id,
            "exact_route": grant.exact_route_id,
            "route_configuration_revision": grant.route_configuration_revision,
            "authorization_expires_at": grant.expires_at.isoformat(),
            "state": "active",
            "phases": [
                {"name": "authorization", "state": "succeeded"},
                {"name": "route_eligibility", "state": "succeeded"},
                {"name": "admission", "state": "succeeded"},
                {"name": "provider", "state": "active"},
                {"name": "accounting", "state": "pending"},
            ],
            "status_url": result.receipt["status_url"],
        }

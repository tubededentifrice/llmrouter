"""Authenticate and coordinate administration embed sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from llmrouter_backend.authority import (
    Audience,
    AuthorityPath,
    EmbedPrincipal,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    SafeAuthorityError,
    Scope,
    ScopeKind,
    ServicePrincipal,
    authorize,
)
from llmrouter_backend.machine_identity import MachineIdentityError

from .model import (
    BootstrapRequest,
    CreatedSession,
    EmbedSessionError,
    EmbedSessionRequest,
    RedeemedSession,
)

if TYPE_CHECKING:
    from .repository import EmbedSessionRepository


class MachineAuthenticator(Protocol):
    """Resolve one opaque host-backend access token."""

    def authenticate(
        self, token: str, *, request_id: str, now: datetime
    ) -> ServicePrincipal:
        """Return one validated machine principal."""
        ...


class EmbedSessionService:
    """Apply machine authority before embed-session storage access."""

    def __init__(
        self,
        authenticator: MachineAuthenticator,
        repository: EmbedSessionRepository,
    ) -> None:
        """Use explicit machine identity and durable session dependencies."""
        self._authenticator = authenticator
        self._repository = repository

    def create(
        self,
        token: str,
        service_id: str,
        document: EmbedSessionRequest,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> CreatedSession:
        """Create one session only for exact host-backend authority."""
        current = now or datetime.now(UTC)
        try:
            principal = self._authenticator.authenticate(
                token, request_id=request_id, now=current
            )
            if (
                document.workspace_id is None
                and principal.allowed_workspace_ids is not None
            ):
                code = "insufficient_scope"
                raise EmbedSessionError(code, request_id)
            scope = Scope(service_id, document.workspace_id)
            context = authorize(
                principal,
                OperationPolicy(
                    operation="admin_embed.create",
                    authority_path=AuthorityPath.MACHINE,
                    principal_kinds=frozenset({PrincipalKind.SERVICE}),
                    scope_kind=ScopeKind.SERVICE_OR_WORKSPACE,
                    machine_audience=Audience.HOST_BACKEND,
                    mutation=True,
                ),
                scope,
                request_id=request_id,
                now=current,
            )
        except MachineIdentityError as error:
            raise EmbedSessionError(error.code, request_id) from error
        except SafeAuthorityError as error:
            raise EmbedSessionError(error.code.value, request_id) from error
        return self._repository.create(context, document, now=current)

    def redeem(
        self,
        session_id: str,
        document: BootstrapRequest,
        *,
        request_origin: str,
        request_id: str,
        now: datetime | None = None,
    ) -> RedeemedSession:
        """Redeem one same-origin bootstrap secret."""
        return self._repository.redeem(
            session_id,
            document.bootstrap_token.get_secret_value(),
            document.frame_nonce,
            document.host_origin,
            request_origin=request_origin,
            request_id=request_id,
            now=now or datetime.now(UTC),
        )

    def authenticate_session(
        self,
        session_token: str,
        *,
        request_origin: str,
        request_id: str,
        now: datetime | None = None,
    ) -> EmbedPrincipal:
        """Authenticate one frame cookie for the exact Router origin."""
        return self._repository.authenticate_session(
            session_token,
            request_origin=request_origin,
            request_id=request_id,
            now=now or datetime.now(UTC),
        )

    def authorize_session(  # noqa: PLR0913
        self,
        session_token: str,
        operation: str,
        scope: Scope,
        *,
        request_origin: str,
        request_id: str,
        now: datetime | None = None,
    ) -> RequestContext:
        """Authorize one frame read through the embed authority path."""
        current = now or datetime.now(UTC)
        try:
            principal = self.authenticate_session(
                session_token,
                request_origin=request_origin,
                request_id=request_id,
                now=current,
            )
            if principal.allowed_workspace_ids and (
                scope.workspace_id not in principal.allowed_workspace_ids
            ):
                code = "insufficient_scope"
                raise EmbedSessionError(code, request_id)
            return authorize(
                principal,
                OperationPolicy(
                    operation=operation,
                    authority_path=AuthorityPath.EMBED,
                    principal_kinds=frozenset({PrincipalKind.EMBED}),
                    scope_kind=scope.kind,
                ),
                scope,
                request_id=request_id,
                now=current,
            )
        except SafeAuthorityError as error:
            raise EmbedSessionError(error.code.value, request_id) from error

    def revoke(
        self,
        token: str,
        service_id: str,
        session_id: str,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        """Revoke one exact session through its host-backend authority."""
        current = now or datetime.now(UTC)
        try:
            principal = self._authenticator.authenticate(
                token, request_id=request_id, now=current
            )
            context = authorize(
                principal,
                OperationPolicy(
                    operation="admin_embed.create",
                    authority_path=AuthorityPath.MACHINE,
                    principal_kinds=frozenset({PrincipalKind.SERVICE}),
                    scope_kind=ScopeKind.SERVICE,
                    machine_audience=Audience.HOST_BACKEND,
                    mutation=True,
                ),
                Scope(service_id),
                request_id=request_id,
                now=current,
            )
        except MachineIdentityError as error:
            raise EmbedSessionError(error.code, request_id) from error
        except SafeAuthorityError as error:
            raise EmbedSessionError(error.code.value, request_id) from error
        self._repository.revoke(
            context,
            session_id,
            now=current,
            allowed_workspace_ids=principal.allowed_workspace_ids,
        )

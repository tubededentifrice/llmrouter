"""Scope-safe builders for authority and hidden-record tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llmrouter_backend.authority import (
    AdministratorPrincipal,
    Audience,
    AuthorityClass,
    EmbedPrincipal,
    RequestContext,
    Scope,
    ServicePrincipal,
)


@dataclass(frozen=True, slots=True)
class ScopeTestBuilder:
    """Build identities only for one fixed test scope."""

    scope: Scope
    now: datetime = datetime(2026, 8, 13, tzinfo=UTC)

    def service(
        self,
        *operations: str,
        audience: Audience = Audience.DATA_PLANE,
        allow_all_workspaces: bool = False,
    ) -> ServicePrincipal:
        """Build one service token without a second scope input."""
        if self.scope.service_id is None:
            msg = "A service principal needs a service test scope."
            raise ValueError(msg)
        allowed_workspaces = (
            None
            if allow_all_workspaces
            else frozenset(
                () if self.scope.workspace_id is None else (self.scope.workspace_id,)
            )
        )
        return ServicePrincipal(
            issuer="test-router",
            token_id="test-token",  # noqa: S106  # nosec B106
            audience=audience,
            service_id=self.scope.service_id,
            operations=frozenset(operations),
            issued_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            credential_generation=1,
            allowed_workspace_ids=allowed_workspaces,
        )

    def administrator(
        self,
        *operations: str,
        global_authority: bool = False,
        recent_authentication_at: datetime | None = None,
    ) -> AdministratorPrincipal:
        """Build one local grant from only the fixed test scope."""
        if global_authority:
            if self.scope != Scope():
                msg = "A global administrator needs a global test scope."
                raise ValueError(msg)
            authority_class = AuthorityClass.GLOBAL_ADMINISTRATOR
            allowed_services = None
            allowed_workspaces = None
        else:
            if self.scope.service_id is None:
                msg = "A service administrator needs a service test scope."
                raise ValueError(msg)
            authority_class = AuthorityClass.SERVICE
            allowed_services = frozenset((self.scope.service_id,))
            allowed_workspaces = (
                frozenset()
                if self.scope.workspace_id is None
                else frozenset((self.scope.workspace_id,))
            )
        return AdministratorPrincipal(
            issuer="https://identity.test",
            subject="test-subject",
            authority_class=authority_class,
            operations=frozenset(operations),
            authenticated_at=self.now - timedelta(hours=1),
            last_activity_at=self.now,
            recent_authentication_at=recent_authentication_at,
            provider_session_checked_at=self.now,
            idle_expires_at=self.now + timedelta(minutes=15),
            absolute_expires_at=self.now + timedelta(hours=7),
            grant_revision=1,
            allowed_service_ids=allowed_services,
            allowed_workspace_ids=allowed_workspaces,
        )

    def embed(
        self,
        *operations: str,
        recent_auth_at: datetime | None = None,
    ) -> EmbedPrincipal:
        """Build one embed session from only the fixed test scope."""
        if self.scope.service_id is None:
            msg = "An embed session needs a service test scope."
            raise ValueError(msg)
        workspaces = frozenset(
            () if self.scope.workspace_id is None else (self.scope.workspace_id,)
        )
        return EmbedPrincipal(
            session_id="test-embed-session",
            host_subject="test-host-subject",
            service_id=self.scope.service_id,
            allowed_workspace_ids=workspaces,
            operations=frozenset(operations),
            issued_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            recent_auth_at=recent_auth_at,
        )


class ScopeSafeRecords[T]:
    """A test record set that accepts only its fixed authorized scope."""

    def __init__(self, scope: Scope) -> None:
        """Bind all later records to one scope."""
        self._scope = scope
        self._records: dict[str, T] = {}

    def add(self, record_id: str, value: T) -> None:
        """Add a record without an optional second scope."""
        if not record_id:
            msg = "The record identity must not be empty."
            raise ValueError(msg)
        self._records[record_id] = value

    def lookup(self, context: RequestContext, record_id: str) -> T | None:
        """Fail a test that accidentally uses a different context scope."""
        if context.scope != self._scope:
            msg = "The test record scope does not match the request context."
            raise ValueError(msg)
        return self._records.get(record_id)

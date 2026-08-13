"""Immutable, duplicate-safe audit event construction and emission."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING

from llmrouter_backend.authority.enforcement import (
    authorize,
    require_expected_revision,
)
from llmrouter_backend.authority.errors import SafeAuthorityError, idempotency_conflict
from llmrouter_backend.authority.model import (
    AdministratorPrincipal,
    AuthorityClass,
    EmbedPrincipal,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from llmrouter_backend.authority.enforcement import (
        IdempotencyRegistry,
        MutationPrecondition,
    )
    from llmrouter_backend.authority.model import (
        BrowserWriteProof,
        OperationPolicy,
        Principal,
        RequestContext,
        Scope,
    )


class AuditClass(StrEnum):
    """Audit retention classes in the accepted data model."""

    SECURITY = "security"
    GLOBAL_ADMINISTRATION = "global_administration"
    AGENT_RUN = "agent_run"
    BUSINESS_TOOL = "business_tool"


class PermissionResult(StrEnum):
    """The result of the permission decision for an audit event."""

    PERMITTED = "permitted"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditSafeDetail:
    """The closed set of safe optional audit details."""

    resource_type: str | None = None
    resource_id: str | None = None
    reason: str | None = None
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        """Apply the formal safe-detail field limits."""
        _bounded(self.resource_type, 100, "resource type")
        _bounded(self.resource_id, 200, "resource identity")
        _bounded(self.reason, 500, "reason")
        _bounded(self.safe_error_code, 100, "safe error code")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One complete immutable audit event."""

    event_id: str
    audit_class: AuditClass
    actor_kind: str
    actor_id: str
    authority_class: AuthorityClass
    scope: Scope
    action: str
    permission_result: PermissionResult
    occurred_at: datetime
    safe_detail: AuditSafeDetail = field(default_factory=AuditSafeDetail)

    def __post_init__(self) -> None:
        """Reject incomplete identities and naive event times."""
        for value, label in (
            (self.event_id, "event identity"),
            (self.actor_kind, "actor kind"),
            (self.actor_id, "actor identity"),
            (self.action, "action"),
        ):
            if not value:
                msg = f"The {label} must not be empty."
                raise ValueError(msg)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            msg = "The event time must include a time zone."
            raise ValueError(msg)


class ImmutableAuditLog:
    """A thread-safe append-once audit sink for process-local use and tests."""

    def __init__(self) -> None:
        """Create an empty ordered event log."""
        self._lock = RLock()
        self._events: dict[str, AuditEvent] = {}

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        """Return an immutable event snapshot in append order."""
        with self._lock:
            return tuple(self._events.values())

    def append_once(self, event: AuditEvent, *, request_id: str) -> bool:
        """Append once, accept an equal replay, and reject changed content."""
        with self._lock:
            existing = self._events.get(event.event_id)
            if existing is None:
                self._events[event.event_id] = event
                return True
            if existing != event:
                raise idempotency_conflict(request_id)
            return False

    def append_with[T](
        self,
        event: AuditEvent,
        operation: Callable[[], T],
        *,
        request_id: str,
    ) -> T:
        """Run local mutation work only when its event identity is free."""
        with self._lock:
            if event.event_id in self._events:
                raise idempotency_conflict(request_id)
            value = operation()
            self._events[event.event_id] = event
            return value


class AuditEmitter:
    """Create structured events only from an authorized request context."""

    def __init__(self, sink: ImmutableAuditLog) -> None:
        """Use the supplied duplicate-safe sink."""
        self._sink = sink

    def emit(
        self,
        context: RequestContext,
        *,
        event_id: str,
        audit_class: AuditClass,
        permission_result: PermissionResult,
        detail: AuditSafeDetail | None = None,
    ) -> AuditEvent:
        """Emit one safe event for the authorized operation."""
        event = _event_from_context(
            context,
            event_id=event_id,
            audit_class=audit_class,
            permission_result=permission_result,
            detail=detail,
        )
        self._sink.append_once(event, request_id=context.request_id)
        return event

    def emit_with[T](
        self,
        context: RequestContext,
        operation: Callable[[], T],
        *,
        event_id: str,
        audit_class: AuditClass,
        detail: AuditSafeDetail | None = None,
    ) -> tuple[T, AuditEvent]:
        """Run local work and append its prevalidated event as one critical section."""
        event = _event_from_context(
            context,
            event_id=event_id,
            audit_class=audit_class,
            permission_result=PermissionResult.PERMITTED,
            detail=detail,
        )
        value = self._sink.append_with(
            event,
            operation,
            request_id=context.request_id,
        )
        return value, event

    def emit_denied(  # noqa: PLR0913
        self,
        principal: Principal,
        policy: OperationPolicy,
        scope: Scope,
        error: SafeAuthorityError,
        *,
        event_id: str,
        audit_class: AuditClass,
        occurred_at: datetime,
    ) -> AuditEvent:
        """Emit one safe event for a denied sensitive action."""
        event = AuditEvent(
            event_id=event_id,
            audit_class=audit_class,
            actor_kind=principal.kind.value,
            actor_id=_principal_actor_id(principal),
            authority_class=(
                principal.authority_class
                if isinstance(principal, AdministratorPrincipal)
                else AuthorityClass.SERVICE
            ),
            scope=scope,
            action=policy.operation,
            permission_result=PermissionResult.DENIED,
            occurred_at=occurred_at,
            safe_detail=AuditSafeDetail(safe_error_code=error.code.value),
        )
        self._sink.append_once(event, request_id=error.request_id)
        return event


@dataclass(frozen=True, slots=True)
class AuditedMutationResult[T]:
    """One stable mutation result and its immutable audit event."""

    value: T
    event: AuditEvent
    replayed: bool


class AuditedMutationExecutor:
    """Apply local idempotency and audit to one authorized mutation."""

    def __init__(
        self,
        registry: IdempotencyRegistry,
        emitter: AuditEmitter,
    ) -> None:
        """Use one registry and one structured emitter."""
        self._registry = registry
        self._emitter = emitter

    def execute[T](  # noqa: PLR0913
        self,
        context: RequestContext,
        precondition: MutationPrecondition,
        operation: Callable[[], T],
        *,
        current_revision: str,
        event_id: str,
        audit_class: AuditClass,
        detail: AuditSafeDetail | None = None,
    ) -> AuditedMutationResult[T]:
        """Run a permitted mutation once and emit exactly one safe event."""
        if not context.mutation:
            msg = "An audited mutation needs a mutation operation policy."
            raise ValueError(msg)

        def apply_and_audit() -> tuple[T, AuditEvent]:
            require_expected_revision(
                precondition,
                current_revision,
                request_id=context.request_id,
            )
            return self._emitter.emit_with(
                context,
                operation,
                event_id=event_id,
                audit_class=audit_class,
                detail=detail,
            )

        result = self._registry.execute(context, precondition, apply_and_audit)
        value, event = result.value
        return AuditedMutationResult(
            value=value,
            event=event,
            replayed=result.replayed,
        )


class SensitivePermissionAuditor:
    """Authorize a sensitive action and audit its permission decision."""

    def __init__(self, emitter: AuditEmitter) -> None:
        """Use the supplied structured audit emitter."""
        self._emitter = emitter

    def authorize(  # noqa: PLR0913
        self,
        principal: Principal,
        policy: OperationPolicy,
        scope: Scope,
        *,
        request_id: str,
        now: datetime,
        event_id: str,
        audit_class: AuditClass,
        browser_write_proof: BrowserWriteProof | None = None,
    ) -> RequestContext:
        """Audit a sensitive denial or a permitted non-mutation action."""
        if not policy.sensitive:
            msg = "The sensitive denial auditor needs a sensitive policy."
            raise ValueError(msg)
        try:
            context = authorize(
                principal,
                policy,
                scope,
                request_id=request_id,
                now=now,
                browser_write_proof=browser_write_proof,
            )
            if not policy.mutation:
                self._emitter.emit(
                    context,
                    event_id=event_id,
                    audit_class=audit_class,
                    permission_result=PermissionResult.PERMITTED,
                )
        except SafeAuthorityError as error:
            self._emitter.emit_denied(
                principal,
                policy,
                scope,
                error,
                event_id=event_id,
                audit_class=audit_class,
                occurred_at=now,
            )
            raise
        else:
            return context


def _event_from_context(
    context: RequestContext,
    *,
    event_id: str,
    audit_class: AuditClass,
    permission_result: PermissionResult,
    detail: AuditSafeDetail | None,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        audit_class=audit_class,
        actor_kind=context.actor_kind.value,
        actor_id=context.actor_id,
        authority_class=context.authority_class,
        scope=context.scope,
        action=context.operation,
        permission_result=permission_result,
        occurred_at=context.authorized_at,
        safe_detail=detail if detail is not None else AuditSafeDetail(),
    )


def _principal_actor_id(principal: Principal) -> str:
    if isinstance(principal, AdministratorPrincipal):
        return f"{len(principal.issuer)}:{principal.issuer}{principal.subject}"
    if isinstance(principal, EmbedPrincipal):
        return principal.session_id
    return principal.service_id


def _bounded(value: str | None, limit: int, label: str) -> None:
    if value is not None and (not value or len(value) > limit):
        msg = f"The audit {label} must contain 1 to {limit} characters."
        raise ValueError(msg)

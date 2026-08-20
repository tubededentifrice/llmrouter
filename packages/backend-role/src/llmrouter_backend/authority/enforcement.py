"""Deny-by-default authority, lookup, and mutation precondition checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hmac import compare_digest
from threading import RLock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from llmrouter_backend.authority.errors import (
    SafeAuthorityError,
    hidden_not_found,
    idempotency_conflict,
    insufficient_scope,
    invalid_token,
    recent_auth_required,
    revision_conflict,
    service_scope_mismatch,
    workspace_scope_mismatch,
)
from llmrouter_backend.authority.model import (
    AdministratorPrincipal,
    AuthorityClass,
    BrowserWriteProof,
    EmbedPrincipal,
    OperationPolicy,
    Principal,
    RequestContext,
    Scope,
    ScopeKind,
    ScopeMismatchMode,
    ServicePrincipal,
)

TOKEN_CLOCK_SKEW = timedelta(seconds=30)
RECENT_AUTH_LIMIT = timedelta(minutes=5)
PROVIDER_SESSION_CHECK_LIMIT = timedelta(minutes=5)
MINIMUM_IDEMPOTENCY_KEY_LENGTH = 16
MAXIMUM_IDEMPOTENCY_KEY_LENGTH = 200
SHA256_HEX_LENGTH = 64


def authorize(  # noqa: PLR0913
    principal: Principal,
    policy: OperationPolicy,
    target_scope: Scope,
    *,
    request_id: str,
    now: datetime,
    browser_write_proof: BrowserWriteProof | None = None,
) -> RequestContext:
    """Resolve all authority before a handler can look up a record."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "The authorization time must include a time zone."
        raise ValueError(msg)
    if not request_id:
        msg = "The request identity must not be empty."
        raise ValueError(msg)
    _check_authority_path(principal, policy, request_id=request_id)
    if principal.revoked:
        raise invalid_token(request_id)
    _check_time(principal, policy, request_id=request_id, now=now)
    if policy.operation not in principal.operations:
        raise insufficient_scope(request_id)
    if not _scope_kind_matches(policy.scope_kind, target_scope.kind):
        raise _scope_error(policy, principal, target_scope, request_id)
    if not _scope_is_allowed(principal, target_scope):
        raise _scope_error(policy, principal, target_scope, request_id)
    _check_browser_write(
        principal,
        policy,
        browser_write_proof,
        request_id=request_id,
    )
    return RequestContext(
        request_id=request_id,
        actor_kind=principal.kind,
        actor_id=_actor_id(principal),
        authority_class=_authority_class(principal),
        authority_path=policy.authority_path,
        machine_audience=policy.machine_audience,
        operation=policy.operation,
        scope=target_scope,
        authorized_at=now,
        recent_authentication_at=_recent_authentication_at(principal),
        mutation=policy.mutation,
    )


def resolve_and_lookup[T](  # noqa: PLR0913
    principal: Principal,
    policy: OperationPolicy,
    target_scope: Scope,
    lookup: Callable[[], T | None],
    *,
    request_id: str,
    now: datetime,
    browser_write_proof: BrowserWriteProof | None = None,
) -> tuple[RequestContext, T]:
    """Authorize first, then return a record or the same hidden error."""
    context = authorize(
        principal,
        policy,
        target_scope,
        request_id=request_id,
        now=now,
        browser_write_proof=browser_write_proof,
    )
    record = lookup()
    if record is None:
        raise hidden_not_found(request_id)
    return context, record


def _check_time(
    principal: Principal,
    policy: OperationPolicy,
    *,
    request_id: str,
    now: datetime,
) -> None:
    if isinstance(principal, ServicePrincipal):
        if principal.expires_at + TOKEN_CLOCK_SKEW < now:
            raise invalid_token(request_id)
        if principal.issued_at - TOKEN_CLOCK_SKEW > now:
            raise invalid_token(request_id)
    elif isinstance(principal, EmbedPrincipal):
        if principal.expires_at <= now or principal.issued_at > now:
            raise invalid_token(request_id)
    if isinstance(principal, AdministratorPrincipal):
        _check_administrator_time(principal, policy, request_id=request_id, now=now)
    if (
        isinstance(principal, EmbedPrincipal)
        and policy.sensitive
        and (
            principal.recent_auth_at is None
            or principal.recent_auth_at > now
            or now - principal.recent_auth_at > RECENT_AUTH_LIMIT
        )
    ):
        raise recent_auth_required(request_id)


def _check_authority_path(
    principal: Principal, policy: OperationPolicy, *, request_id: str
) -> None:
    if principal.kind not in policy.principal_kinds:
        raise invalid_token(request_id)
    if principal.authority_path is not policy.authority_path:
        raise invalid_token(request_id)
    if isinstance(principal, ServicePrincipal):
        if principal.audience is not policy.machine_audience:
            raise invalid_token(request_id)
    elif policy.machine_audience is not None:
        raise invalid_token(request_id)


def _check_browser_write(
    principal: Principal,
    policy: OperationPolicy,
    proof: BrowserWriteProof | None,
    *,
    request_id: str,
) -> None:
    """Require exact Origin and session-bound CSRF values for administrator writes."""
    if not isinstance(principal, AdministratorPrincipal) or not policy.mutation:
        return
    if (
        proof is None
        or proof.request_origin != proof.allowed_origin
        or not compare_digest(proof.request_csrf_token, proof.session_csrf_token)
    ):
        raise insufficient_scope(request_id)


def _check_administrator_time(
    principal: AdministratorPrincipal,
    policy: OperationPolicy,
    *,
    request_id: str,
    now: datetime,
) -> None:
    if principal.authenticated_at > now:
        raise invalid_token(request_id)
    if principal.last_activity_at > now:
        raise invalid_token(request_id)
    if principal.idle_expires_at <= now or principal.absolute_expires_at <= now:
        raise invalid_token(request_id)
    if (
        principal.provider_session_checked_at > now
        or now - principal.provider_session_checked_at > PROVIDER_SESSION_CHECK_LIMIT
    ):
        raise invalid_token(request_id)
    if policy.sensitive and (
        principal.recent_authentication_at is None
        or principal.recent_authentication_at > now
        or now - principal.recent_authentication_at > RECENT_AUTH_LIMIT
    ):
        raise recent_auth_required(request_id)


def _scope_kind_matches(required: ScopeKind, actual: ScopeKind) -> bool:
    if required is ScopeKind.SERVICE_OR_WORKSPACE:
        return actual in {ScopeKind.SERVICE, ScopeKind.WORKSPACE}
    return required is actual


def _scope_error(
    policy: OperationPolicy,
    principal: Principal,
    scope: Scope,
    request_id: str,
) -> SafeAuthorityError:
    if policy.scope_mismatch_mode is ScopeMismatchMode.HIDDEN_RECORD:
        return hidden_not_found(request_id)

    if isinstance(principal, (ServicePrincipal, EmbedPrincipal)):
        if scope.service_id != principal.service_id:
            return service_scope_mismatch(request_id)
    elif scope.service_id is None or (
        principal.allowed_service_ids is not None
        and scope.service_id not in principal.allowed_service_ids
    ):
        return service_scope_mismatch(request_id)

    if scope.workspace_id is not None:
        return workspace_scope_mismatch(request_id)
    return service_scope_mismatch(request_id)


def _scope_is_allowed(principal: Principal, scope: Scope) -> bool:
    if isinstance(principal, AdministratorPrincipal):
        return _administrator_scope_is_allowed(principal, scope)
    if isinstance(principal, ServicePrincipal):
        return _service_scope_is_allowed(principal, scope)
    return _embed_scope_is_allowed(principal, scope)


def _administrator_scope_is_allowed(
    principal: AdministratorPrincipal, scope: Scope
) -> bool:
    if (
        principal.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
        and scope.kind is ScopeKind.GLOBAL
    ):
        return (
            principal.allowed_service_ids is None
            and principal.allowed_workspace_ids is None
        )
    service_allowed = scope.service_id is not None and (
        principal.allowed_service_ids is None
        or scope.service_id in principal.allowed_service_ids
    )
    workspace_allowed = scope.workspace_id is None or (
        principal.allowed_workspace_ids is None
        or scope.workspace_id in principal.allowed_workspace_ids
    )
    return service_allowed and workspace_allowed


def _service_scope_is_allowed(principal: ServicePrincipal, scope: Scope) -> bool:
    return scope.service_id == principal.service_id and (
        scope.workspace_id is None
        or principal.allowed_workspace_ids is None
        or scope.workspace_id in principal.allowed_workspace_ids
    )


def _embed_scope_is_allowed(principal: EmbedPrincipal, scope: Scope) -> bool:
    if scope.workspace_id is None and not principal.allowed_workspace_ids:
        return scope.service_id == principal.service_id and principal.operations <= {
            "configuration.read",
            "configuration.write",
            "health.read",
        }
    return scope.service_id == principal.service_id and (
        scope.workspace_id is None
        or scope.workspace_id in principal.allowed_workspace_ids
    )


def _actor_id(principal: Principal) -> str:
    if isinstance(principal, ServicePrincipal):
        return principal.service_id
    if isinstance(principal, AdministratorPrincipal):
        return f"{len(principal.issuer)}:{principal.issuer}{principal.subject}"
    return principal.session_id


def _authority_class(principal: Principal) -> AuthorityClass:
    if isinstance(principal, AdministratorPrincipal):
        return principal.authority_class
    return AuthorityClass.SERVICE


def _recent_authentication_at(principal: Principal) -> datetime | None:
    if isinstance(principal, AdministratorPrincipal):
        return principal.recent_authentication_at
    if isinstance(principal, EmbedPrincipal):
        return principal.recent_auth_at
    return None


@dataclass(frozen=True, slots=True)
class MutationPrecondition:
    """One expected revision and one content-bound idempotency key."""

    expected_revision: str
    idempotency_key: str
    fingerprint_sha256: str

    def __post_init__(self) -> None:
        """Reject weak or malformed mutation preconditions."""
        if not self.expected_revision:
            msg = "The expected revision must not be empty."
            raise ValueError(msg)
        if not (
            MINIMUM_IDEMPOTENCY_KEY_LENGTH
            <= len(self.idempotency_key)
            <= MAXIMUM_IDEMPOTENCY_KEY_LENGTH
        ):
            msg = "The idempotency key must contain 16 to 200 characters."
            raise ValueError(msg)
        if len(self.fingerprint_sha256) != SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in self.fingerprint_sha256
        ):
            msg = "The mutation fingerprint must be a lowercase SHA-256 value."
            raise ValueError(msg)


def require_expected_revision(
    precondition: MutationPrecondition,
    current_revision: str,
    *,
    request_id: str,
) -> None:
    """Reject a stale write before it changes state."""
    if precondition.expected_revision != current_revision:
        raise revision_conflict(request_id)


@dataclass(frozen=True, slots=True)
class IdempotentResult[T]:
    """The stable result of an idempotent operation."""

    value: T
    replayed: bool


@dataclass(frozen=True, slots=True)
class _StoredResult[T]:
    fingerprint_sha256: str
    value: T


class IdempotencyRegistry:
    """A deterministic atomic registry for handlers and tests.

    A durable implementation must use the same key and fingerprint rules in
    the database transaction that changes state.
    """

    def __init__(self) -> None:
        """Create an empty registry with one local atomic boundary."""
        self._lock = RLock()
        self._results: dict[
            tuple[
                AuthorityClass,
                str,
                str | None,
                str,
                Scope,
                str,
                str,
            ],
            _StoredResult[object],
        ] = {}

    def execute[T](
        self,
        context: RequestContext,
        precondition: MutationPrecondition,
        operation: Callable[[], T],
    ) -> IdempotentResult[T]:
        """Run one operation or return its equal concurrent replay."""
        key = (
            context.authority_class,
            context.authority_path.value,
            context.machine_audience.value
            if context.machine_audience is not None
            else None,
            context.actor_id,
            context.scope,
            context.operation,
            precondition.idempotency_key,
        )
        with self._lock:
            stored = self._results.get(key)
            if stored is not None:
                if stored.fingerprint_sha256 != precondition.fingerprint_sha256:
                    raise idempotency_conflict(context.request_id)
                return IdempotentResult(value=cast("T", stored.value), replayed=True)
            value = operation()
            self._results[key] = _StoredResult(
                fingerprint_sha256=precondition.fingerprint_sha256,
                value=value,
            )
            return IdempotentResult(value=value, replayed=False)

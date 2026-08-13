"""Immutable authority values for one authenticated Router request."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class Audience(StrEnum):
    """Accepted service-token audiences that do not share authority."""

    DATA_PLANE = "data_plane"
    SERVICE_MANAGEMENT = "service_management"
    HOST_BACKEND = "host_backend"
    ACCOUNTING = "accounting"
    CONFIGURATION = "configuration"
    BUDGET_AUTHORITY = "budget_authority"


MACHINE_OPERATIONS_BY_AUDIENCE: dict[Audience, frozenset[str]] = {
    Audience.DATA_PLANE: frozenset(
        {
            "model.create",
            "model.read",
            "model.cancel",
            "run.create",
            "run.read",
            "run.cancel",
            "tool.create",
            "tool.read",
            "tool.cancel",
            "attachment.create",
            "attachment.read",
            "embedding.create",
            "embedding.read",
        }
    ),
    Audience.SERVICE_MANAGEMENT: frozenset(
        {
            "workspace.create",
            "workspace.read",
            "workspace.disable",
            "workspace.restore",
            "workspace.retire",
        }
    ),
    Audience.HOST_BACKEND: frozenset({"admin_embed.create"}),
    Audience.ACCOUNTING: frozenset({"accounting.read"}),
    Audience.CONFIGURATION: frozenset(
        {
            "configuration.read",
            "configuration.write",
            "diagnostic.grant.create",
            "retention.read",
            "retention.preview",
            "retention.write",
            "budget.read",
            "budget.write",
        }
    ),
    Audience.BUDGET_AUTHORITY: frozenset(
        {"budget_ceiling.read", "budget_ceiling.write"}
    ),
}

ADMINISTRATOR_OPERATIONS = frozenset(
    {
        "service.manage",
        "service_parent.manage",
        "catalog.manage",
        "provider_instance.manage",
        "provider_route.manage",
        "business_tool_gateway.approve",
        "credential.manage",
        "assignment.manage",
        "budget.read",
        "budget.write",
        "accounting.read",
        "retention.manage",
        "grant.manage",
        "audit.read",
        "content.read",
        "export.create",
        "health.read",
        "node.drain",
        "circuit.probe",
        "circuit.reset",
        "high_availability.promote",
        "high_availability.failback",
        "backup.start",
        "restore.validate",
        "disaster_recovery.test",
    }
)

EMBED_OPERATIONS = frozenset(
    {
        "configuration.read",
        "configuration.write",
        "budget.read",
        "budget.write",
        "accounting.read",
        "request_status.read",
        "health.read",
        "diagnostic.run",
    }
)

SENSITIVE_ADMINISTRATOR_OPERATIONS = frozenset(
    {
        "business_tool_gateway.approve",
        "content.read",
        "high_availability.promote",
        "high_availability.failback",
        "restore.validate",
    }
)
SENSITIVE_ADMINISTRATOR_MUTATIONS = frozenset(
    {
        "credential.manage",
        "export.create",
        "grant.manage",
        "retention.manage",
        "service.manage",
        "service_parent.manage",
    }
)
SENSITIVE_EMBED_OPERATIONS = frozenset(
    {"configuration.write", "budget.write", "diagnostic.run"}
)

SERVICE_TOKEN_LIFETIME = timedelta(minutes=5)
ADMINISTRATOR_IDLE_LIMIT = timedelta(minutes=15)
ADMINISTRATOR_ABSOLUTE_LIMIT = timedelta(hours=8)
EMBED_RECENT_AUTH_LIMIT = timedelta(minutes=5)


class AuthorityPath(StrEnum):
    """Authentication paths that cannot authenticate to each other."""

    MACHINE = "machine"
    GLOBAL_ADMINISTRATION = "global_administration"
    EMBED = "embed"


class PrincipalKind(StrEnum):
    """Authenticated principal types with separate permission paths."""

    SERVICE = "service"
    ADMINISTRATOR = "administrator"
    EMBED = "embed"
    SYSTEM = "system"


class AuthorityClass(StrEnum):
    """Authority classes that can occur in an audit scope."""

    SERVICE = "service"
    GLOBAL_ADMINISTRATOR = "global_administrator"
    SYSTEM = "system"


class ScopeKind(StrEnum):
    """The exact scope shape that an operation accepts."""

    GLOBAL = "global"
    SERVICE = "service"
    WORKSPACE = "workspace"
    SERVICE_OR_WORKSPACE = "service_or_workspace"


class ScopeMismatchMode(StrEnum):
    """The public result for an authenticated scope mismatch."""

    EXPLICIT = "explicit"
    HIDDEN_RECORD = "hidden_record"


@dataclass(frozen=True, slots=True)
class Scope:
    """One global, service, or service-workspace scope."""

    service_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        """Reject partial and empty scoped identities."""
        if self.service_id == "" or self.workspace_id == "":
            msg = "Scope identities must not be empty."
            raise ValueError(msg)
        if self.workspace_id is not None and self.service_id is None:
            msg = "A workspace scope must include its service identity."
            raise ValueError(msg)

    @property
    def kind(self) -> ScopeKind:
        """Return the exact kind of this scope."""
        if self.workspace_id is not None:
            return ScopeKind.WORKSPACE
        if self.service_id is not None:
            return ScopeKind.SERVICE
        return ScopeKind.GLOBAL


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    """One short-lived service-token identity."""

    issuer: str
    token_id: str
    audience: Audience
    service_id: str
    operations: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    credential_generation: int
    allowed_workspace_ids: frozenset[str] | None = None
    revoked: bool = False
    kind: PrincipalKind = field(default=PrincipalKind.SERVICE, init=False)
    authority_path: AuthorityPath = field(default=AuthorityPath.MACHINE, init=False)

    def __post_init__(self) -> None:
        """Reject malformed token claims before authorization."""
        _require_text(self.issuer, "issuer")
        _require_text(self.token_id, "token identity")
        _require_text(self.service_id, "service identity")
        _require_operations(self.operations)
        _require_aware(self.issued_at, "issue time")
        _require_aware(self.expires_at, "expiry time")
        if self.expires_at <= self.issued_at:
            msg = "The token expiry must be after its issue time."
            raise ValueError(msg)
        if self.expires_at - self.issued_at != SERVICE_TOKEN_LIFETIME:
            msg = "The service-token lifetime must be five minutes."
            raise ValueError(msg)
        if self.credential_generation < 1:
            msg = "The credential generation must be positive."
            raise ValueError(msg)
        if self.allowed_workspace_ids is not None:
            for workspace_id in self.allowed_workspace_ids:
                _require_text(workspace_id, "workspace identity")
        if not self.operations <= MACHINE_OPERATIONS_BY_AUDIENCE[self.audience]:
            msg = "Each service-token operation must match its exact audience."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AdministratorPrincipal:
    """One local administrator session and one effective local grant."""

    issuer: str
    subject: str
    authority_class: AuthorityClass
    operations: frozenset[str]
    authenticated_at: datetime
    last_activity_at: datetime
    recent_authentication_at: datetime | None
    account_checked_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    grant_revision: int
    allowed_service_ids: frozenset[str] | None = None
    allowed_workspace_ids: frozenset[str] | None = None
    revoked: bool = False
    kind: PrincipalKind = field(default=PrincipalKind.ADMINISTRATOR, init=False)
    authority_path: AuthorityPath = field(
        default=AuthorityPath.GLOBAL_ADMINISTRATION, init=False
    )

    def __post_init__(self) -> None:  # noqa: C901
        """Reject a grant with an invalid class, scope, or time."""
        _require_text(self.issuer, "issuer")
        _require_text(self.subject, "subject")
        _require_operations(self.operations)
        _require_aware(self.authenticated_at, "authentication time")
        _require_aware(self.last_activity_at, "last-activity time")
        if self.recent_authentication_at is not None:
            _require_aware(self.recent_authentication_at, "recent authentication time")
        _require_aware(self.account_checked_at, "account-check time")
        _require_aware(self.idle_expires_at, "idle-expiry time")
        _require_aware(self.absolute_expires_at, "absolute-expiry time")
        if not self.authenticated_at <= self.last_activity_at:
            msg = "The last-activity time must not precede authentication."
            raise ValueError(msg)
        if not self.last_activity_at < self.idle_expires_at:
            msg = "The idle expiry must be after the last activity."
            raise ValueError(msg)
        if self.idle_expires_at - self.last_activity_at > ADMINISTRATOR_IDLE_LIMIT:
            msg = "The administrator idle lifetime must not exceed 15 minutes."
            raise ValueError(msg)
        if not self.authenticated_at < self.absolute_expires_at:
            msg = "The absolute expiry must be after authentication."
            raise ValueError(msg)
        if (
            self.absolute_expires_at - self.authenticated_at
            > ADMINISTRATOR_ABSOLUTE_LIMIT
        ):
            msg = "The administrator absolute lifetime must not exceed eight hours."
            raise ValueError(msg)
        if self.idle_expires_at > self.absolute_expires_at:
            msg = "The idle expiry must not exceed the absolute expiry."
            raise ValueError(msg)
        if self.account_checked_at < self.authenticated_at:
            msg = "The account-check time must not precede authentication."
            raise ValueError(msg)
        if (
            self.recent_authentication_at is not None
            and self.recent_authentication_at < self.authenticated_at
        ):
            msg = "The recent-authentication time must not precede authentication."
            raise ValueError(msg)
        if self.grant_revision < 1:
            msg = "The grant revision must be positive."
            raise ValueError(msg)
        _validate_administrator_scope(self)
        if not self.operations <= ADMINISTRATOR_OPERATIONS:
            msg = "An administrator grant contains an unsupported operation."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EmbedPrincipal:
    """One host-authorized, short-lived embedded-view session."""

    session_id: str
    host_subject: str
    service_id: str
    allowed_workspace_ids: frozenset[str]
    operations: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    recent_auth_at: datetime | None = None
    revoked: bool = False
    kind: PrincipalKind = field(default=PrincipalKind.EMBED, init=False)
    authority_path: AuthorityPath = field(default=AuthorityPath.EMBED, init=False)

    def __post_init__(self) -> None:
        """Reject an embed session with malformed immutable claims."""
        _require_text(self.session_id, "session identity")
        _require_text(self.host_subject, "host subject")
        _require_text(self.service_id, "service identity")
        _require_operations(self.operations)
        _require_aware(self.issued_at, "issue time")
        _require_aware(self.expires_at, "expiry time")
        if self.expires_at <= self.issued_at:
            msg = "The session expiry must be after its issue time."
            raise ValueError(msg)
        if self.expires_at - self.issued_at > EMBED_RECENT_AUTH_LIMIT:
            msg = "An embed session must not live for more than five minutes."
            raise ValueError(msg)
        if self.recent_auth_at is not None:
            _require_aware(self.recent_auth_at, "recent authentication time")
            if self.recent_auth_at < self.issued_at:
                msg = "The recent-authentication time must not precede session issue."
                raise ValueError(msg)
            if self.expires_at > self.recent_auth_at + EMBED_RECENT_AUTH_LIMIT:
                msg = (
                    "A recently authenticated embed session must expire in five "
                    "minutes."
                )
                raise ValueError(msg)
        for workspace_id in self.allowed_workspace_ids:
            _require_text(workspace_id, "workspace identity")
        if not self.operations <= EMBED_OPERATIONS:
            msg = "An embed session contains an unsupported operation."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BrowserWriteProof:
    """Browser values that the authority boundary must compare for a write."""

    allowed_origin: str
    request_origin: str
    session_csrf_token: str
    request_csrf_token: str

    def __post_init__(self) -> None:
        """Reject absent browser control values before comparison."""
        _require_text(self.allowed_origin, "allowed origin")
        _require_text(self.request_origin, "request origin")
        _require_text(self.session_csrf_token, "session CSRF token")
        _require_text(self.request_csrf_token, "request CSRF token")


type Principal = ServicePrincipal | AdministratorPrincipal | EmbedPrincipal


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    """The declared authority rule for one route operation."""

    operation: str
    authority_path: AuthorityPath
    principal_kinds: frozenset[PrincipalKind]
    scope_kind: ScopeKind
    machine_audience: Audience | None = None
    scope_mismatch_mode: ScopeMismatchMode = ScopeMismatchMode.HIDDEN_RECORD
    sensitive: bool = False
    mutation: bool = False

    def __post_init__(self) -> None:  # noqa: C901
        """Require an exact operation and at least one principal type."""
        _require_text(self.operation, "operation")
        if not self.principal_kinds:
            msg = "An operation policy must allow a principal type."
            raise ValueError(msg)
        if (self.authority_path is AuthorityPath.MACHINE) != (
            self.machine_audience is not None
        ):
            msg = "Only a machine authority policy can declare a machine audience."
            raise ValueError(msg)
        expected_kinds = {
            AuthorityPath.MACHINE: frozenset({PrincipalKind.SERVICE}),
            AuthorityPath.GLOBAL_ADMINISTRATION: frozenset(
                {PrincipalKind.ADMINISTRATOR}
            ),
            AuthorityPath.EMBED: frozenset({PrincipalKind.EMBED}),
        }[self.authority_path]
        if self.principal_kinds != expected_kinds:
            msg = (
                "An operation policy must use the exact principal for its authority "
                "path."
            )
            raise ValueError(msg)
        if self.authority_path is AuthorityPath.MACHINE:
            audience = self.machine_audience
            if audience is None:
                msg = "A machine operation policy must declare an audience."
                raise ValueError(msg)
            if self.operation not in MACHINE_OPERATIONS_BY_AUDIENCE[audience]:
                msg = "The operation does not match the machine audience."
                raise ValueError(msg)
        elif self.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION:
            if self.operation not in ADMINISTRATOR_OPERATIONS:
                msg = "The administrator operation is not in the public contract."
                raise ValueError(msg)
            requires_recent_auth = (
                self.operation in SENSITIVE_ADMINISTRATOR_OPERATIONS
                or (
                    self.mutation
                    and self.operation in SENSITIVE_ADMINISTRATOR_MUTATIONS
                )
            )
            if requires_recent_auth and not self.sensitive:
                msg = "The administrator operation must require recent authentication."
                raise ValueError(msg)
        elif self.operation not in EMBED_OPERATIONS:
            msg = "The embed operation is not in the public contract."
            raise ValueError(msg)
        elif self.operation in SENSITIVE_EMBED_OPERATIONS and not self.sensitive:
            msg = "The embed operation must require recent authentication."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """The normalized result of one successful authority decision."""

    request_id: str
    actor_kind: PrincipalKind
    actor_id: str
    authority_class: AuthorityClass
    authority_path: AuthorityPath
    machine_audience: Audience | None
    operation: str
    scope: Scope
    authorized_at: datetime
    recent_authentication_at: datetime | None
    mutation: bool


def _require_text(value: str, label: str) -> None:
    if not value:
        msg = f"The {label} must not be empty."
        raise ValueError(msg)


def _require_operations(operations: frozenset[str]) -> None:
    if not operations:
        msg = "At least one exact operation is required."
        raise ValueError(msg)
    for operation in operations:
        _require_text(operation, "operation")


def _validate_administrator_scope(principal: AdministratorPrincipal) -> None:
    if principal.allowed_service_ids is not None:
        for service_id in principal.allowed_service_ids:
            _require_text(service_id, "service identity")
    if principal.allowed_workspace_ids is not None:
        for workspace_id in principal.allowed_workspace_ids:
            _require_text(workspace_id, "workspace identity")
    if principal.authority_class is AuthorityClass.SERVICE:
        if not principal.allowed_service_ids:
            msg = "A service administrator grant must allow a service."
            raise ValueError(msg)
    elif principal.authority_class is not AuthorityClass.GLOBAL_ADMINISTRATOR:
        msg = "A human session cannot use system authority."
        raise ValueError(msg)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"The {label} must include a time zone."
        raise ValueError(msg)

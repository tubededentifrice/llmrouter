"""Secret-safe values for external identity and local administrator grants."""
# ruff: noqa: C901, PLR2004

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from llmrouter_backend.authority import ADMINISTRATOR_OPERATIONS, AuthorityClass, Scope

if TYPE_CHECKING:
    from datetime import datetime


class AuthenticationPurpose(StrEnum):
    """Supported interactive authentication purposes."""

    LOGIN = "login"
    RECENT_AUTHENTICATION = "recent_authentication"


class TrustedGrantPurpose(StrEnum):
    """Trusted-console local grant purposes."""

    INITIAL = "initial"
    RECOVERY = "recovery"


class GrantState(StrEnum):
    """Public local grant states."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """One show-once 256-bit unpadded base64url value."""

    value: str

    def __post_init__(self) -> None:
        """Reject a value outside the generated secret form."""
        alphabet = frozenset(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        try:
            decoded = base64.b64decode(self.value + "=", altchars=b"-_", validate=True)
        except ValueError as error:
            msg = "A generated secret must be 32-byte unpadded base64url."
            raise ValueError(msg) from error
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode()
        if (
            len(decoded) != 32
            or canonical != self.value
            or not set(self.value) <= alphabet
        ):
            msg = "A generated secret must be 32-byte unpadded base64url."
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Do not expose the value in logs or diagnostics."""
        return "SecretValue([REDACTED])"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ProviderSessionState:
    """Current provider session state and its rotated opaque tokens."""

    active: bool
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    checked_at: datetime

    def __post_init__(self) -> None:
        """Require bounded opaque tokens and aware ordered times."""
        if (
            not self.access_token
            or not self.refresh_token
            or len(self.access_token) > 8192
            or len(self.refresh_token) > 8192
        ):
            msg = "Provider session tokens are required."
            raise ValueError(msg)
        for value in (self.access_expires_at, self.checked_at):
            if value.tzinfo is None or value.utcoffset() is None:
                msg = "Provider session times need a time zone."
                raise ValueError(msg)

    def __repr__(self) -> str:
        """Do not expose identity-provider tokens in diagnostics."""
        return "ProviderSessionState([REDACTED])"


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """One fully validated Pocket ID identity token result."""

    issuer: str
    subject: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    authenticated_at: datetime

    def __post_init__(self) -> None:
        """Require immutable bounded identity and ordered aware times."""
        if not self.issuer or not 1 <= len(self.subject) <= 200 or not self.nonce:
            msg = "A verified identity has invalid immutable claims."
            raise ValueError(msg)
        for value in (self.issued_at, self.expires_at, self.authenticated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                msg = "Verified identity times must include a time zone."
                raise ValueError(msg)
        if self.authenticated_at > self.issued_at or self.issued_at >= self.expires_at:
            msg = "Verified identity times are not ordered."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class OIDCTokenResponse:
    """The transient result of one authorization-code exchange."""

    id_token: str
    token_type: str
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None

    def __repr__(self) -> str:
        """Do not expose identity-provider tokens in diagnostics."""
        return "OIDCTokenResponse([REDACTED])"


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    """One public authorization redirect without server-held secrets."""

    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TrustedGrantURL:
    """One show-once trusted-console grant URL."""

    url: str
    expires_at: datetime

    def __repr__(self) -> str:
        """Do not expose the one-use verifier in logs."""
        return "TrustedGrantURL([REDACTED])"


@dataclass(frozen=True, slots=True)
class AdministratorGrant:
    """One explicit local administrator grant."""

    grant_id: str
    issuer: str
    subject: str
    authority_class: AuthorityClass
    operations: frozenset[str]
    service_id: str | None
    workspace_ids: frozenset[str]
    state: GrantState
    revision: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    def __post_init__(self) -> None:
        """Reject open operations and invalid scope shapes."""
        if not self.operations or not self.operations <= ADMINISTRATOR_OPERATIONS:
            msg = "Each grant operation must be in the public contract."
            raise ValueError(msg)
        if self.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR:
            if self.service_id is not None or self.workspace_ids:
                msg = "A global administrator grant must have global scope."
                raise ValueError(msg)
        elif self.authority_class is AuthorityClass.SERVICE:
            if self.service_id is None:
                msg = "A service administrator grant needs a service."
                raise ValueError(msg)
        else:
            msg = "A human grant cannot use system authority."
            raise ValueError(msg)
        if not self.grant_id or not self.issuer or not 1 <= len(self.subject) <= 200:
            msg = "A grant has an invalid immutable identity."
            raise ValueError(msg)
        if not 1 <= len(self.revision) <= 200:
            msg = "A grant revision must contain 1 to 200 characters."
            raise ValueError(msg)
        for value in (self.created_at, self.expires_at, self.revoked_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                msg = "Grant times must include a time zone."
                raise ValueError(msg)
        if self.expires_at is not None and self.expires_at <= self.created_at:
            msg = "A grant expiry must be after creation."
            raise ValueError(msg)

    @property
    def scope(self) -> Scope:
        """Return the grant's broad service or global scope."""
        return Scope(self.service_id)


@dataclass(frozen=True, slots=True)
class SessionResult:
    """One local session result with show-once browser secrets."""

    session_token: SecretValue | None
    csrf_token: SecretValue | None
    issuer: str
    subject: str
    grants: tuple[str, ...]
    authenticated_at: datetime
    recent_authentication_at: datetime | None
    account_state_checked_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    return_path: str
    identity_account_url: str

    def __post_init__(self) -> None:
        """Require bounded identity, exact local return path, and ordered times."""
        if not self.issuer or not 1 <= len(self.subject) <= 200:
            msg = "A session has an invalid immutable identity."
            raise ValueError(msg)
        if len(set(self.grants)) != len(self.grants) or any(
            not 1 <= len(grant_id) <= 200 for grant_id in self.grants
        ):
            msg = "Session grants must contain unique bounded identities."
            raise ValueError(msg)
        if not self.identity_account_url.startswith("https://"):
            msg = "The identity account URL must use HTTPS."
            raise ValueError(msg)
        if not self.return_path.startswith("/") or self.return_path.startswith("//"):
            msg = "A session return path must remain local."
            raise ValueError(msg)
        values = (
            self.authenticated_at,
            self.account_state_checked_at,
            self.idle_expires_at,
            self.absolute_expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            msg = "Session times must include a time zone."
            raise ValueError(msg)
        if not self.authenticated_at < self.idle_expires_at <= self.absolute_expires_at:
            msg = "Session expiry times are not ordered."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class GrantRequest:
    """One proposed least-privilege local grant."""

    issuer: str
    subject: str
    authority_class: AuthorityClass
    operations: frozenset[str]
    reason: str
    service_id: str | None = None
    workspace_ids: frozenset[str] = frozenset()
    expires_at: datetime | None = None
    expected_revision: str | None = None

    def __post_init__(self) -> None:
        """Apply the same closed operation and scope rules as a stored grant."""
        if not self.issuer or not self.subject:
            msg = "A grant needs an immutable issuer and subject."
            raise ValueError(msg)
        if not self.operations or not self.operations <= ADMINISTRATOR_OPERATIONS:
            msg = "Each grant operation must be in the public contract."
            raise ValueError(msg)
        if self.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR:
            if self.service_id is not None or self.workspace_ids:
                msg = "A global grant must have global scope."
                raise ValueError(msg)
        elif self.authority_class is AuthorityClass.SERVICE:
            if self.service_id is None:
                msg = "A service grant needs a service."
                raise ValueError(msg)
        else:
            msg = "A human grant cannot use system authority."
            raise ValueError(msg)
        if not 1 <= len(self.subject) <= 200 or len(self.issuer) > 2000:
            msg = "A grant identity is outside the public limits."
            raise ValueError(msg)
        if not 1 <= len(self.reason) <= 500:
            msg = "A grant reason must contain 1 to 500 characters."
            raise ValueError(msg)
        if len(self.workspace_ids) > 1000:
            msg = "A grant can contain no more than 1000 workspaces."
            raise ValueError(msg)
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            msg = "A grant expiry must include a time zone."
            raise ValueError(msg)
        if (
            self.expected_revision is not None
            and not 1 <= len(self.expected_revision) <= 200
        ):
            msg = "An expected revision must contain 1 to 200 characters."
            raise ValueError(msg)

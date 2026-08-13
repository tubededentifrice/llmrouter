"""Secret-safe values for service bootstrap and machine access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from llmrouter_backend.authority import MACHINE_OPERATIONS_BY_AUDIENCE, Audience

if TYPE_CHECKING:
    from datetime import datetime

MAXIMUM_ROTATION_OVERLAP = timedelta(hours=24)
DEFAULT_ROTATION_OVERLAP = MAXIMUM_ROTATION_OVERLAP
MAXIMUM_TLS_CERTIFICATE_LIFETIME = timedelta(hours=24)
MAXIMUM_WORKSPACE_IDS = 1000
SECRET_VALUE_LENGTH = 43
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class WorkspaceLimit(StrEnum):
    """The workspace range that one bootstrap generation can grant."""

    ALL_SERVICE_WORKSPACES = "all_service_workspaces"
    EXPLICIT_ONLY = "explicit_only"


class DigestKeyCustodyState(StrEnum):
    """The safe operator state for token-digest key custody."""

    NORMAL = "normal"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class DigestKeyCustodyStatus:
    """A safe report of active token rows that need unavailable digest keys."""

    state: DigestKeyCustodyState
    missing_key_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BootstrapScope:
    """The maximum closed authority of one bootstrap generation."""

    audiences: frozenset[Audience]
    operations: frozenset[str]
    workspace_limit: WorkspaceLimit = WorkspaceLimit.ALL_SERVICE_WORKSPACES

    def __post_init__(self) -> None:
        """Reject empty, cross-audience, or malformed authority."""
        if not self.audiences or not self.operations:
            msg = "A bootstrap scope needs an audience and an operation."
            raise ValueError(msg)
        permitted = frozenset().union(
            *(MACHINE_OPERATIONS_BY_AUDIENCE[audience] for audience in self.audiences)
        )
        if not self.operations <= permitted:
            msg = "A bootstrap operation must match an allowed audience."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """One transient show-once secret with a redacted representation."""

    value: str

    def __post_init__(self) -> None:
        """Require the unambiguous 256-bit base64url form."""
        if len(self.value) != SECRET_VALUE_LENGTH or not set(self.value) <= (
            _BASE64URL_ALPHABET
        ):
            msg = "A generated secret must be 32-byte unpadded base64url."
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Do not expose the secret in logs or diagnostics."""
        return "SecretValue([REDACTED])"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class BootstrapCreated:
    """The show-once result for a new bootstrap generation."""

    service_id: str
    generation: int
    secret: SecretValue
    prior_generation_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TokenExchange:
    """The public five-minute token exchange response."""

    access_token: SecretValue
    service_id: str
    audience: Audience
    operations: frozenset[str]
    credential_generation: int
    workspace_ids: frozenset[str] | None = None
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int = 300


@dataclass(frozen=True, slots=True)
class TLSClientIdentity:
    """Validated TLS facts supplied by the trusted server ingress."""

    certificate_identity: str
    service_id: str
    credential_generation: int
    tls_version: str
    private_trust_anchor: bool
    server_certificate_validated: bool
    client_certificate_validated: bool
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        """Require the accepted production TLS profile."""
        if not self.certificate_identity or not self.service_id:
            msg = "A TLS identity must include a certificate and service identity."
            raise ValueError(msg)
        if self.credential_generation < 1:
            msg = "A TLS credential generation must be positive."
            raise ValueError(msg)
        if self.tls_version != "TLSv1.3":
            msg = "A machine TLS identity must use TLS 1.3."
            raise ValueError(msg)
        if not (
            self.private_trust_anchor
            and self.server_certificate_validated
            and self.client_certificate_validated
        ):
            msg = "A machine TLS identity must use validated private trust."
            raise ValueError(msg)
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            msg = "Machine TLS certificate times must include a time zone."
            raise ValueError(msg)
        if self.expires_at <= self.issued_at or (
            self.expires_at - self.issued_at > MAXIMUM_TLS_CERTIFICATE_LIFETIME
        ):
            msg = "A machine TLS certificate must live for at most 24 hours."
            raise ValueError(msg)

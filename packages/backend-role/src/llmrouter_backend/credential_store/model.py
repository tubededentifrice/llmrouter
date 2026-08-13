"""Closed secret and metadata values for encrypted credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from datetime import datetime

MAXIMUM_SECRET_CHARACTERS = 65_536
MAXIMUM_SAFE_LABEL_CHARACTERS = 200
MAXIMUM_REASON_CHARACTERS = 500


class CredentialState(StrEnum):
    """Public credential lifecycle states."""

    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class CredentialAction(StrEnum):
    """Accepted public credential changes."""

    ROTATE = "rotate"
    DISABLE = "disable"
    RETIRE = "retire"


class WrappingKeyCustodyState(StrEnum):
    """Safe operator states for wrapping-key custody."""

    NORMAL = "normal"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True, repr=False)
class SecretInput:
    """One transient write-only credential value."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the accepted write-only string bound."""
        if not 1 <= len(self.value) <= MAXIMUM_SECRET_CHARACTERS:
            msg = "A credential secret must contain from 1 to 65536 characters."
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Do not expose the value in logs or diagnostics."""
        return "SecretInput([REDACTED])"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class CredentialOwner:
    """One global or service credential owner."""

    service_id: str | None = None

    @property
    def public_scope(self) -> str:
        """Return the provider-neutral public owner identity."""
        return "global" if self.service_id is None else self.service_id


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    """Safe credential metadata without recoverable secret material."""

    credential_id: str
    owner_scope: str
    provider_catalog_id: str
    state: CredentialState
    revision: str
    created_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CredentialResult:
    """One create result that states if it is a durable replay."""

    metadata: CredentialMetadata
    replayed: bool


@dataclass(frozen=True, slots=True)
class WrappingKeyCustodyStatus:
    """A safe report of credential rows that need unavailable keys."""

    state: WrappingKeyCustodyState
    missing_key_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UrgentInvalidation:
    """One ordered credential-cache invalidation."""

    sequence: int
    credential_id: str
    generation: int
    action: CredentialAction
    occurred_at: datetime


@dataclass(slots=True, repr=False, weakref_slot=True, eq=False)
class SecretLease:
    """One bounded decrypted value that can erase its mutable buffer."""

    credential_id: str
    generation: int
    expires_at: datetime
    _value: bytearray = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def read(self, *, now: datetime) -> memoryview:
        """Return a read-only view while the lease is current."""
        if self._closed or now >= self.expires_at:
            self.close()
            msg = "The credential lease is not available."
            raise RuntimeError(msg)
        return memoryview(self._value).toreadonly()

    def close(self) -> None:
        """Erase this lease buffer one time."""
        if not self._closed:
            self._value[:] = bytes(len(self._value))
            self._closed = True

    @property
    def closed(self) -> bool:
        """State if this lease no longer contains usable material."""
        return self._closed

    def __enter__(self) -> Self:
        """Return this lease for a bounded use block."""
        return self

    def __exit__(self, *_unused: object) -> None:
        """Erase the value at the end of a use block."""
        self.close()

    def __repr__(self) -> str:
        """Do not expose decrypted material."""
        return "SecretLease([REDACTED])"

    __str__ = __repr__

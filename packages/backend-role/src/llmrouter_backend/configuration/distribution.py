"""Authenticated node-local configuration revision distribution."""
# ruff: noqa: D105, D107, EM101, TRY003

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING

import rfc8785

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

MAXIMUM_NORMAL_REVISION_AGE = timedelta(hours=24)
_AUTHENTICATION_TAG_BYTES = hashlib.sha256().digest_size
_MINIMUM_AUTHENTICATION_KEY_BYTES = 32
_MAXIMUM_POLICY_REVISION_CHARACTERS = 200
_NORMAL_DOMAIN = b"llmrouter.configuration.normal.v1\x00"
_URGENT_DOMAIN = b"llmrouter.configuration.urgent.v1\x00"


class DistributionErrorCode(StrEnum):
    """Safe configuration distribution failure codes."""

    INVALID_REVISION = "invalid_configuration_revision"
    UNAUTHENTICATED = "unauthenticated_configuration_revision"
    REVISION_ROLLBACK = "configuration_revision_rollback"
    URGENT_REVISION_PENDING = "urgent_configuration_revision_pending"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    CONFIGURATION_STALE = "configuration_stale"
    CONFIGURATION_MISMATCH = "configuration_revision_mismatch"
    SERVICE_DISABLED = "service_disabled"
    WORKSPACE_DISABLED = "workspace_disabled"
    CREDENTIAL_REVOKED = "credential_revoked"
    SECURITY_POLICY_BLOCKED = "security_policy_blocked"


class ConfigurationDistributionError(RuntimeError):
    """One safe configuration distribution failure."""

    __slots__ = ("code",)

    def __init__(self, code: DistributionErrorCode) -> None:
        messages = {
            DistributionErrorCode.INVALID_REVISION: (
                "The configuration revision is invalid."
            ),
            DistributionErrorCode.UNAUTHENTICATED: (
                "The configuration revision is not authenticated."
            ),
            DistributionErrorCode.REVISION_ROLLBACK: (
                "The configuration revision is not newer than active state."
            ),
            DistributionErrorCode.URGENT_REVISION_PENDING: (
                "An urgent configuration revision must apply first."
            ),
            DistributionErrorCode.CONFIGURATION_UNAVAILABLE: (
                "Configuration is not available for this request."
            ),
            DistributionErrorCode.CONFIGURATION_STALE: (
                "Configuration is too old for new work."
            ),
            DistributionErrorCode.CONFIGURATION_MISMATCH: (
                "The active configuration does not match this request."
            ),
            DistributionErrorCode.SERVICE_DISABLED: (
                "The service is not available for new work."
            ),
            DistributionErrorCode.WORKSPACE_DISABLED: (
                "The workspace is not available for new work."
            ),
            DistributionErrorCode.CREDENTIAL_REVOKED: (
                "A required provider credential is not available."
            ),
            DistributionErrorCode.SECURITY_POLICY_BLOCKED: (
                "Security policy does not permit new work."
            ),
        }
        super().__init__(messages[code])
        self.code = code


class DistributionSafetyState(StrEnum):
    """The safe node-local state for one request scope."""

    CURRENT = "current"
    STALE = "stale"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DistributionScope:
    """One exact service or workspace distribution scope."""

    service_id: str
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.service_id)
        if self.workspace_id is not None:
            _require_uuid(self.workspace_id)


@dataclass(frozen=True, slots=True)
class CredentialGeneration:
    """One exact credential generation and owning service scope."""

    credential_id: str
    generation: int
    owner_service_id: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.credential_id)
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("A credential generation must be positive.")
        if self.owner_service_id is not None:
            _require_uuid(self.owner_service_id)


@dataclass(frozen=True, slots=True)
class NormalConfigurationRevision:
    """One immutable normal revision for an exact request scope."""

    scope: DistributionScope
    revision_id: str
    revision_number: int
    content_sha256: bytes
    published_at: datetime
    required_urgent_sequence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DistributionScope):
            raise TypeError("A normal configuration scope is invalid.")
        _require_uuid(self.revision_id)
        if (
            not isinstance(self.revision_number, int)
            or isinstance(self.revision_number, bool)
            or self.revision_number < 1
        ):
            raise ValueError("A normal configuration revision must be positive.")
        if (
            not isinstance(self.content_sha256, bytes)
            or len(self.content_sha256) != hashlib.sha256().digest_size
        ):
            raise ValueError("A configuration digest must be SHA-256.")
        _require_aware(self.published_at)
        if (
            not isinstance(self.required_urgent_sequence, int)
            or isinstance(self.required_urgent_sequence, bool)
            or self.required_urgent_sequence < 0
        ):
            raise ValueError("An urgent revision watermark must not be negative.")


@dataclass(frozen=True, slots=True)
class UrgentConfigurationRevision:
    """One complete immutable urgent security-state snapshot."""

    sequence: int
    disabled_service_ids: frozenset[str]
    disabled_workspace_scopes: frozenset[DistributionScope]
    revoked_credentials: frozenset[CredentialGeneration]
    security_policy_revision: str
    admission_allowed: bool
    published_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "disabled_service_ids", frozenset(self.disabled_service_ids)
        )
        object.__setattr__(
            self,
            "disabled_workspace_scopes",
            frozenset(self.disabled_workspace_scopes),
        )
        object.__setattr__(
            self, "revoked_credentials", frozenset(self.revoked_credentials)
        )
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("An urgent configuration sequence must be positive.")
        for service_id in self.disabled_service_ids:
            _require_uuid(service_id)
        if any(
            not isinstance(scope, DistributionScope) or scope.workspace_id is None
            for scope in self.disabled_workspace_scopes
        ):
            raise ValueError("An urgent workspace scope must name one workspace.")
        if not isinstance(self.security_policy_revision, str) or not (
            1
            <= len(self.security_policy_revision)
            <= _MAXIMUM_POLICY_REVISION_CHARACTERS
        ):
            raise ValueError("A security policy revision is invalid.")
        if not isinstance(self.admission_allowed, bool):
            raise TypeError("An urgent admission policy must be true or false.")
        if any(
            not isinstance(item, CredentialGeneration)
            for item in self.revoked_credentials
        ):
            raise ValueError("An urgent credential revision is invalid.")
        _require_aware(self.published_at)


@dataclass(frozen=True, slots=True)
class AuthenticatedNormalRevision:
    """One normal revision with a private transport authentication tag."""

    revision: NormalConfigurationRevision
    authentication_challenge: bytes = field(repr=False)
    authentication_tag: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.revision, NormalConfigurationRevision):
            raise TypeError("An authenticated normal revision is invalid.")
        _require_authentication_challenge(self.authentication_challenge)
        if (
            not isinstance(self.authentication_tag, bytes)
            or len(self.authentication_tag) != _AUTHENTICATION_TAG_BYTES
        ):
            raise ValueError("A revision authentication tag is invalid.")


@dataclass(frozen=True, slots=True)
class AuthenticatedUrgentRevision:
    """One urgent revision with a private transport authentication tag."""

    revision: UrgentConfigurationRevision
    authentication_challenge: bytes = field(repr=False)
    authentication_tag: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.revision, UrgentConfigurationRevision):
            raise TypeError("An authenticated urgent revision is invalid.")
        _require_authentication_challenge(self.authentication_challenge)
        if (
            not isinstance(self.authentication_tag, bytes)
            or len(self.authentication_tag) != _AUTHENTICATION_TAG_BYTES
        ):
            raise ValueError("A revision authentication tag is invalid.")


class RevisionAuthenticator:
    """Authenticate domain-separated canonical revision payloads."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < _MINIMUM_AUTHENTICATION_KEY_BYTES:
            raise ValueError("The revision authentication key is too short.")
        self._key = bytes(key)

    def normal(
        self,
        revision: NormalConfigurationRevision,
        *,
        authentication_challenge: bytes,
    ) -> AuthenticatedNormalRevision:
        """Authenticate one normal revision for distribution."""
        _require_authentication_challenge(authentication_challenge)
        return AuthenticatedNormalRevision(
            revision,
            authentication_challenge,
            hmac.digest(
                self._key,
                _normal_message(revision, authentication_challenge),
                "sha256",
            ),
        )

    def urgent(
        self,
        revision: UrgentConfigurationRevision,
        *,
        authentication_challenge: bytes,
    ) -> AuthenticatedUrgentRevision:
        """Authenticate one urgent revision for distribution."""
        _require_authentication_challenge(authentication_challenge)
        return AuthenticatedUrgentRevision(
            revision,
            authentication_challenge,
            hmac.digest(
                self._key,
                _urgent_message(revision, authentication_challenge),
                "sha256",
            ),
        )

    def verify_normal(
        self,
        value: AuthenticatedNormalRevision,
        *,
        authentication_challenge: bytes,
    ) -> bool:
        """Verify a normal revision in constant time."""
        challenge_matches = hmac.compare_digest(
            authentication_challenge, value.authentication_challenge
        )
        expected = hmac.digest(
            self._key,
            _normal_message(value.revision, value.authentication_challenge),
            "sha256",
        )
        tag_matches = hmac.compare_digest(expected, value.authentication_tag)
        return challenge_matches & tag_matches

    def verify_urgent(
        self,
        value: AuthenticatedUrgentRevision,
        *,
        authentication_challenge: bytes,
    ) -> bool:
        """Verify an urgent revision in constant time."""
        challenge_matches = hmac.compare_digest(
            authentication_challenge, value.authentication_challenge
        )
        expected = hmac.digest(
            self._key,
            _urgent_message(value.revision, value.authentication_challenge),
            "sha256",
        )
        tag_matches = hmac.compare_digest(expected, value.authentication_tag)
        return challenge_matches & tag_matches


@dataclass(frozen=True, slots=True)
class DistributionStatus:
    """Bounded safe distribution state for health and administration."""

    scope: DistributionScope
    active_revision: str | None
    received_at: datetime | None
    age: timedelta | None
    stale: bool
    safety_state: DistributionSafetyState
    urgent_sequence: int
    security_policy_revision: str | None


@dataclass(frozen=True, slots=True)
class AdmissionDistributionSnapshot:
    """The private revision state held through one new admission commit."""

    scope: DistributionScope
    configuration_revision_id: str
    received_at: datetime
    urgent_sequence: int
    security_policy_revision: str | None
    revoked_credentials: frozenset[CredentialGeneration]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revoked_credentials", frozenset(self.revoked_credentials)
        )
        if any(
            not isinstance(item, CredentialGeneration)
            for item in self.revoked_credentials
        ):
            raise TypeError("An admission credential revision is invalid.")

    def require_revision(self, revision_id: str) -> None:
        """Require the resolved target to use the distributed revision."""
        if revision_id != self.configuration_revision_id:
            raise ConfigurationDistributionError(
                DistributionErrorCode.CONFIGURATION_MISMATCH
            )

    def require_credentials(self, credentials: Sequence[CredentialGeneration]) -> None:
        """Reject one exact credential generation from urgent state."""
        if any(item in self.revoked_credentials for item in credentials):
            raise ConfigurationDistributionError(
                DistributionErrorCode.CREDENTIAL_REVOKED
            )


@dataclass(frozen=True, slots=True)
class _ActiveNormalRevision:
    revision: NormalConfigurationRevision
    received_at: datetime


class ConfigurationRevisionDistribution:
    """Keep safe authenticated configuration state for one data-plane node."""

    def __init__(
        self,
        authenticator: RevisionAuthenticator,
        *,
        authentication_challenge: bytes | None = None,
    ) -> None:
        if not isinstance(authenticator, RevisionAuthenticator):
            raise TypeError("A revision authenticator is required.")
        self._authenticator = authenticator
        challenge = (
            secrets.token_bytes(_AUTHENTICATION_TAG_BYTES)
            if authentication_challenge is None
            else authentication_challenge
        )
        _require_authentication_challenge(challenge)
        self._authentication_challenge = bytes(challenge)
        self._lock = RLock()
        self._normal: dict[DistributionScope, _ActiveNormalRevision] = {}
        self._urgent: UrgentConfigurationRevision | None = None

    @property
    def authentication_challenge(self) -> bytes:
        """Return the safe one-process challenge that signed revisions must bind."""
        return self._authentication_challenge

    def apply_normal(
        self, value: AuthenticatedNormalRevision, *, received_at: datetime
    ) -> DistributionStatus:
        """Validate and activate one ordered normal revision."""
        _require_aware(received_at)
        if not self._authenticator.verify_normal(
            value, authentication_challenge=self._authentication_challenge
        ):
            raise ConfigurationDistributionError(DistributionErrorCode.UNAUTHENTICATED)
        revision = value.revision
        if revision.published_at > received_at:
            raise ConfigurationDistributionError(DistributionErrorCode.INVALID_REVISION)
        with self._lock:
            urgent_sequence = 0 if self._urgent is None else self._urgent.sequence
            if revision.required_urgent_sequence > urgent_sequence:
                raise ConfigurationDistributionError(
                    DistributionErrorCode.URGENT_REVISION_PENDING
                )
            active = self._normal.get(revision.scope)
            if active is not None:
                if revision.revision_number < active.revision.revision_number:
                    raise ConfigurationDistributionError(
                        DistributionErrorCode.REVISION_ROLLBACK
                    )
                if revision.revision_number == active.revision.revision_number:
                    if revision != active.revision:
                        raise ConfigurationDistributionError(
                            DistributionErrorCode.INVALID_REVISION
                        )
                    return self._status_locked(revision.scope, received_at, ())
                if revision.revision_id == active.revision.revision_id:
                    raise ConfigurationDistributionError(
                        DistributionErrorCode.INVALID_REVISION
                    )
            self._normal[revision.scope] = _ActiveNormalRevision(revision, received_at)
            return self._status_locked(revision.scope, received_at, ())

    def apply_urgent(
        self, value: AuthenticatedUrgentRevision, *, received_at: datetime
    ) -> None:
        """Apply a complete urgent snapshot before later normal work."""
        _require_aware(received_at)
        if not self._authenticator.verify_urgent(
            value, authentication_challenge=self._authentication_challenge
        ):
            raise ConfigurationDistributionError(DistributionErrorCode.UNAUTHENTICATED)
        revision = value.revision
        if revision.published_at > received_at:
            raise ConfigurationDistributionError(DistributionErrorCode.INVALID_REVISION)
        with self._lock:
            if self._urgent is not None and revision.sequence <= self._urgent.sequence:
                raise ConfigurationDistributionError(
                    DistributionErrorCode.REVISION_ROLLBACK
                )
            self._urgent = revision

    def status(
        self,
        scope: DistributionScope,
        *,
        now: datetime,
        ancestor_service_ids: Sequence[str] = (),
    ) -> DistributionStatus:
        """Return safe active revision, age, stale state, and urgent state."""
        _require_aware(now)
        with self._lock:
            return self._status_locked(scope, now, ancestor_service_ids)

    @contextmanager
    def admission(
        self,
        scope: DistributionScope,
        *,
        now: datetime,
        ancestor_service_ids: Sequence[str],
    ) -> Iterator[AdmissionDistributionSnapshot]:
        """Serialize one new admission with urgent revision application."""
        _require_aware(now)
        self._lock.acquire()
        try:
            active = self._normal.get(scope)
            if active is None:
                raise ConfigurationDistributionError(
                    DistributionErrorCode.CONFIGURATION_UNAVAILABLE
                )
            if (
                now < active.received_at
                or now - active.received_at >= MAXIMUM_NORMAL_REVISION_AGE
            ):
                raise ConfigurationDistributionError(
                    DistributionErrorCode.CONFIGURATION_STALE
                )
            urgent = self._urgent
            if urgent is not None:
                if not urgent.admission_allowed:
                    raise ConfigurationDistributionError(
                        DistributionErrorCode.SECURITY_POLICY_BLOCKED
                    )
                applicable_services = frozenset(ancestor_service_ids) | {
                    scope.service_id
                }
                if any(
                    service_id in urgent.disabled_service_ids
                    for service_id in applicable_services
                ):
                    raise ConfigurationDistributionError(
                        DistributionErrorCode.SERVICE_DISABLED
                    )
                if scope in urgent.disabled_workspace_scopes:
                    raise ConfigurationDistributionError(
                        DistributionErrorCode.WORKSPACE_DISABLED
                    )
            yield AdmissionDistributionSnapshot(
                scope=scope,
                configuration_revision_id=active.revision.revision_id,
                received_at=active.received_at,
                urgent_sequence=0 if urgent is None else urgent.sequence,
                security_policy_revision=(
                    None if urgent is None else urgent.security_policy_revision
                ),
                revoked_credentials=(
                    frozenset() if urgent is None else urgent.revoked_credentials
                ),
            )
        finally:
            self._lock.release()

    def _status_locked(
        self,
        scope: DistributionScope,
        now: datetime,
        ancestor_service_ids: Sequence[str],
    ) -> DistributionStatus:
        active = self._normal.get(scope)
        urgent = self._urgent
        urgent_sequence = 0 if urgent is None else urgent.sequence
        policy_revision = None if urgent is None else urgent.security_policy_revision
        if active is None:
            return DistributionStatus(
                scope=scope,
                active_revision=None,
                received_at=None,
                age=None,
                stale=False,
                safety_state=DistributionSafetyState.UNAVAILABLE,
                urgent_sequence=urgent_sequence,
                security_policy_revision=policy_revision,
            )
        clock_rollback = now < active.received_at
        age = max(now - active.received_at, timedelta(0))
        stale = clock_rollback or age >= MAXIMUM_NORMAL_REVISION_AGE
        applicable_services = frozenset(ancestor_service_ids) | {scope.service_id}
        blocked = urgent is not None and (
            not urgent.admission_allowed
            or any(
                service_id in urgent.disabled_service_ids
                for service_id in applicable_services
            )
            or scope in urgent.disabled_workspace_scopes
        )
        state = (
            DistributionSafetyState.BLOCKED
            if blocked
            else DistributionSafetyState.STALE
            if stale
            else DistributionSafetyState.CURRENT
        )
        return DistributionStatus(
            scope,
            active.revision.revision_id,
            active.received_at,
            age,
            stale,
            state,
            urgent_sequence,
            policy_revision,
        )


def _normal_message(
    revision: NormalConfigurationRevision, authentication_challenge: bytes
) -> bytes:
    return _NORMAL_DOMAIN + rfc8785.dumps(
        {
            "authentication_challenge": authentication_challenge.hex(),
            "content_sha256": revision.content_sha256.hex(),
            "kind": "normal",
            "published_at": _canonical_time(revision.published_at),
            "required_urgent_sequence": revision.required_urgent_sequence,
            "revision_id": revision.revision_id,
            "revision_number": revision.revision_number,
            "scope": {
                "service_id": revision.scope.service_id,
                "workspace_id": revision.scope.workspace_id,
            },
        }
    )


def _urgent_message(
    revision: UrgentConfigurationRevision, authentication_challenge: bytes
) -> bytes:
    return _URGENT_DOMAIN + rfc8785.dumps(
        {
            "admission_allowed": revision.admission_allowed,
            "authentication_challenge": authentication_challenge.hex(),
            "disabled_service_ids": sorted(revision.disabled_service_ids),
            "disabled_workspace_scopes": [
                {"service_id": item.service_id, "workspace_id": item.workspace_id}
                for item in sorted(
                    revision.disabled_workspace_scopes,
                    key=lambda value: (value.service_id, value.workspace_id or ""),
                )
            ],
            "kind": "urgent",
            "published_at": _canonical_time(revision.published_at),
            "revoked_credentials": [
                {
                    "credential_id": item.credential_id,
                    "generation": item.generation,
                    "owner_service_id": item.owner_service_id,
                }
                for item in sorted(
                    revision.revoked_credentials,
                    key=lambda value: (
                        value.credential_id,
                        value.generation,
                        value.owner_service_id or "",
                    ),
                )
            ],
            "security_policy_revision": revision.security_policy_revision,
            "sequence": revision.sequence,
        }
    )


def _canonical_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _require_uuid(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError("A distribution identity must be a UUID.") from error
    if str(parsed) != value:
        raise ValueError("A distribution identity must be a canonical UUID.")


def _require_authentication_challenge(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != _AUTHENTICATION_TAG_BYTES:
        raise ValueError("A revision authentication challenge is invalid.")


def _require_aware(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("A distribution time must include a time zone.")

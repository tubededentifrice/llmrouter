"""Bounded local provider health values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

MAXIMUM_FLEET_HINT_LIFETIME = timedelta(minutes=5)
DEFAULT_FLEET_HINT_LIFETIME = timedelta(seconds=60)
MAXIMUM_HEALTH_WINDOW_SIZE = 100
MAXIMUM_OPEN_DURATION = timedelta(minutes=5)
MAXIMUM_HEALTH_BACKOFF = timedelta(minutes=15)
MAXIMUM_PROBE_LIMIT = 10
MAXIMUM_JITTER_RATIO = 0.5
_MAXIMUM_IDENTITY_CHARACTERS = 500
_MINIMUM_AUTHENTICATION_TAG_BYTES = 16
_MAXIMUM_AUTHENTICATION_TAG_BYTES = 512


class ProviderFailureClass(StrEnum):
    """Provider failure classes that can change local health."""

    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    SERVER = "server"
    RATE_LIMIT = "rate_limit"
    INVALID_RESPONSE = "invalid_response"
    CREDENTIAL = "credential"


class CircuitState(StrEnum):
    """Local provider health circuit states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitSettings:
    """Editable circuit settings inside fixed global safety limits."""

    window_size: int = 10
    minimum_samples: int = 3
    failure_threshold: float = 0.5
    open_duration: timedelta = timedelta(seconds=30)
    probe_limit: int = 1
    maximum_backoff: timedelta = timedelta(minutes=5)
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:  # noqa: C901
        """Reject settings outside the fixed health safety limits."""
        integer_values = (self.window_size, self.minimum_samples, self.probe_limit)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_values
        ):
            message = "Circuit count settings must be integers."
            raise TypeError(message)
        if not 1 <= self.window_size <= MAXIMUM_HEALTH_WINDOW_SIZE:
            message = "The health sample window is outside the fixed limit."
            raise ValueError(message)
        if not 1 <= self.minimum_samples <= self.window_size:
            message = "The minimum sample count must fit in the health window."
            raise ValueError(message)
        if not 1 <= self.probe_limit <= MAXIMUM_PROBE_LIMIT:
            message = "The half-open probe limit is outside the fixed limit."
            raise ValueError(message)
        if isinstance(self.failure_threshold, bool) or not isinstance(
            self.failure_threshold, (int, float)
        ):
            message = "The circuit failure threshold must be a number."
            raise TypeError(message)
        if not 0 < float(self.failure_threshold) <= 1:
            message = "The circuit failure threshold is outside the fixed limit."
            raise ValueError(message)
        if not timedelta(seconds=1) <= self.open_duration <= MAXIMUM_OPEN_DURATION:
            message = "The circuit open duration is outside the fixed limit."
            raise ValueError(message)
        if not self.open_duration <= self.maximum_backoff <= MAXIMUM_HEALTH_BACKOFF:
            message = "The circuit maximum backoff is outside the fixed limit."
            raise ValueError(message)
        if isinstance(self.jitter_ratio, bool) or not isinstance(
            self.jitter_ratio, (int, float)
        ):
            message = "The probe jitter ratio must be a number."
            raise TypeError(message)
        if not 0 <= float(self.jitter_ratio) <= MAXIMUM_JITTER_RATIO:
            message = "The probe jitter ratio is outside the fixed limit."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class HealthScope:
    """The exact provider, credential, route, and operation health scope."""

    provider_instance_id: str
    provider_instance_generation: int
    provider_model_route_id: str
    route_generation: int
    credential_id: str
    credential_generation: int
    operation: str
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Require complete bounded provider, credential, and route identities."""
        _identities(
            self.provider_instance_id,
            self.provider_model_route_id,
            self.credential_id,
            self.operation,
        )
        capabilities = frozenset(self.required_capabilities)
        if any(not isinstance(value, str) for value in capabilities):
            message = "A required health capability must be text."
            raise TypeError(message)
        if capabilities:
            _identities(*capabilities)
        object.__setattr__(self, "required_capabilities", capabilities)
        generations = (
            self.provider_instance_generation,
            self.route_generation,
            self.credential_generation,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in generations
        ):
            message = "Health scope generations must be positive."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CircuitKey:
    """One narrow local circuit identity."""

    scope: HealthScope
    failure_class: ProviderFailureClass

    def __post_init__(self) -> None:
        """Require one closed normalized provider failure class."""
        if not isinstance(self.failure_class, ProviderFailureClass):
            message = "The health failure class is invalid."
            raise TypeError(message)


@dataclass(frozen=True, slots=True)
class FleetHint:
    """One signed expiring advisory suppression from the control plane."""

    key: CircuitKey
    sample_started_at: datetime
    sample_ended_at: datetime
    published_at: datetime
    expires_at: datetime
    authentication_tag: bytes

    def __post_init__(self) -> None:
        """Require one bounded ordered sample and publication window."""
        values = (
            self.sample_started_at,
            self.sample_ended_at,
            self.published_at,
            self.expires_at,
        )
        if any(not isinstance(value, datetime) for value in values):
            message = "Fleet hint times must be date-time values."
            raise TypeError(message)
        started, ended, published, expires = values
        for value in values:
            if value.tzinfo is None or value.utcoffset() is None:
                message = "Fleet hint times must include a time zone."
                raise ValueError(message)
        if not started <= ended <= published < expires:
            message = "The fleet hint time order is invalid."
            raise ValueError(message)
        if expires - published > MAXIMUM_FLEET_HINT_LIFETIME:
            message = "The fleet hint lifetime is outside the fixed limit."
            raise ValueError(message)
        if (
            not isinstance(self.authentication_tag, bytes)
            or not _MINIMUM_AUTHENTICATION_TAG_BYTES
            <= len(self.authentication_tag)
            <= _MAXIMUM_AUTHENTICATION_TAG_BYTES
        ):
            message = "The fleet hint authentication tag is invalid."
            raise ValueError(message)


class FleetHintVerifier(Protocol):
    """Verify one fleet hint against trusted control-plane keys."""

    def __call__(self, hint: FleetHint) -> bool:
        """Return true only for an authentic, unchanged hint."""
        ...


@dataclass(frozen=True, slots=True)
class HealthPermit:
    """One owned health decision and its held half-open probe slots."""

    allowed: bool
    scope: HealthScope
    probe_keys: tuple[CircuitKey, ...] = ()
    reason: str | None = None
    next_probe_at: datetime | None = None
    _decision_token: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Keep denied permits safe and allowed permits owned."""
        if self.allowed and (self.reason is not None or self.next_probe_at is not None):
            message = "An allowed health permit cannot contain a denial."
            raise ValueError(message)
        if not self.allowed and (not self.reason or self.probe_keys):
            message = "A denied health permit must contain one safe reason."
            raise ValueError(message)
        if self.allowed != (self._decision_token is not None):
            message = (
                "An allowed health permit must contain one private decision token."
            )
            raise ValueError(message)
        if self.next_probe_at is not None and not isinstance(
            self.next_probe_at, datetime
        ):
            message = "The next probe time must be a date-time value."
            raise TypeError(message)


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """Safe inspectable local and fleet health state."""

    key: CircuitKey
    local_state: CircuitState
    sample_count: int
    failure_count: int
    sample_started_at: datetime | None
    sample_ended_at: datetime | None
    next_probe_at: datetime | None
    active_probes: int
    last_state_change: datetime
    fleet_hint_active: bool
    fleet_hint_sample_started_at: datetime | None
    fleet_hint_sample_ended_at: datetime | None
    fleet_hint_published_at: datetime | None
    fleet_hint_expires_at: datetime | None


def _identities(*values: str) -> None:
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > _MAXIMUM_IDENTITY_CHARACTERS
        or any(not " " <= character <= "~" for character in value)
        for value in values
    ):
        message = "A health scope identity is invalid."
        raise ValueError(message)

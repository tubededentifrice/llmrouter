"""Provider-neutral immutable route and fallback values."""
# ruff: noqa: C901, PLR0911, PLR0912, PLR0915, PLR2004

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from llmrouter_backend.accounting import PriceComponent, UsageComponent
from llmrouter_backend.execution import AdapterStopEvidence, TerminalError

MAXIMUM_PROVIDER_ATTEMPTS = 8
MAXIMUM_LOGICAL_MILLISECONDS = 900_000
MAXIMUM_ATTEMPT_MILLISECONDS = 120_000
MAXIMUM_CONNECT_MILLISECONDS = 10_000
MAXIMUM_FIRST_BYTE_MILLISECONDS = 30_000
MAXIMUM_IDLE_MILLISECONDS = 30_000
MAXIMUM_DIAGNOSTIC_GRANT_SECONDS = 300
ROUTING_CLAIM_SECONDS = 30
_URL_SAFE_GRANT = re.compile(r"^[A-Za-z0-9_-]{43,200}$")
_MAXIMUM_IDENTITY_CHARACTERS = 500
_MAXIMUM_SAFE_TEXT_CHARACTERS = 1_000


class FallbackDecision(StrEnum):
    """Durable decisions after one candidate or provider result."""

    SUCCEEDED = "succeeded"
    NEXT_CANDIDATE = "next_candidate"
    STOP_REQUEST = "stop_request"
    COMMIT_BOUNDARY = "commit_boundary"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    """Provider attempt terminal outcomes."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class AdapterPhase(StrEnum):
    """Safe provider operation milestones used for timeout enforcement."""

    CONNECTED = "connected"
    FIRST_BYTE = "first_byte"
    PROGRESS = "progress"


@dataclass(frozen=True, slots=True)
class AttemptTimeouts:
    """The four fixed adapter time limits for one attempt."""

    connect_ms: int
    first_byte_ms: int
    idle_ms: int
    execution_ms: int

    def __post_init__(self) -> None:
        """Require exact positive integer limits inside the execution limit."""
        values = (self.connect_ms, self.first_byte_ms, self.idle_ms, self.execution_ms)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            message = "Attempt timeout values must be integers."
            raise TypeError(message)
        if not 100 <= self.execution_ms <= MAXIMUM_ATTEMPT_MILLISECONDS:
            message = "The attempt execution timeout is outside the accepted range."
            raise ValueError(message)
        if not all(1 <= value <= self.execution_ms for value in values[:-1]):
            message = "Each phase timeout must fit inside the execution timeout."
            raise ValueError(message)

    @classmethod
    def fixed_for_execution(cls, execution_ms: int) -> AttemptTimeouts:
        """Derive fixed bounded phase limits from one configured attempt limit."""
        return cls(
            min(MAXIMUM_CONNECT_MILLISECONDS, execution_ms),
            min(MAXIMUM_FIRST_BYTE_MILLISECONDS, execution_ms),
            min(MAXIMUM_IDLE_MILLISECONDS, execution_ms),
            execution_ms,
        )


@dataclass(frozen=True, slots=True)
class DiagnosticGrant:
    """One short-lived exact-route grant returned only at creation."""

    grant_id: str
    grant: str = field(repr=False)
    service_id: str
    workspace_id: str | None
    exact_route_id: str
    route_configuration_revision: str
    expires_at: datetime

    def __post_init__(self) -> None:
        """Require complete bounded scope and one URL-safe bearer value."""
        _identities(
            self.grant_id,
            self.service_id,
            self.exact_route_id,
            self.route_configuration_revision,
        )
        if self.workspace_id is not None:
            _identities(self.workspace_id)
        if _URL_SAFE_GRANT.fullmatch(self.grant) is None:
            message = "The diagnostic grant is not a bounded URL-safe value."
            raise ValueError(message)
        _aware(self.expires_at)


@dataclass(frozen=True, slots=True)
class DiagnosticAuthorization:
    """The checked exact route scope added to a request fingerprint."""

    grant_id: str
    service_id: str
    workspace_id: str | None
    exact_route_id: str
    route_configuration_revision: str

    def __post_init__(self) -> None:
        """Require one complete checked diagnostic scope."""
        _identities(
            self.grant_id,
            self.service_id,
            self.exact_route_id,
            self.route_configuration_revision,
        )
        if self.workspace_id is not None:
            _identities(self.workspace_id)

    def fingerprint_scope(self) -> dict[str, str | None]:
        """Return only the permission scope required by admission."""
        return {
            "service_id": self.service_id,
            "workspace_id": self.workspace_id,
            "exact_route_id": self.exact_route_id,
        }


@dataclass(frozen=True, slots=True)
class SafeFailureEvidence:
    """Closed redacted provider facts that can enter durable evidence."""

    provider_status: int | None = None
    retry_after_ms: int | None = None
    detail_code: str | None = None

    def __post_init__(self) -> None:
        """Bound safe scalar evidence and reject provider content."""
        if self.provider_status is not None and (
            isinstance(self.provider_status, bool)
            or not isinstance(self.provider_status, int)
            or not 100 <= self.provider_status <= 599
        ):
            message = "The safe provider status is invalid."
            raise ValueError(message)
        if self.retry_after_ms is not None and (
            isinstance(self.retry_after_ms, bool)
            or not isinstance(self.retry_after_ms, int)
            or not 0 <= self.retry_after_ms <= 900_000
        ):
            message = "The safe retry delay is invalid."
            raise ValueError(message)
        if self.detail_code is not None and (
            not self.detail_code
            or len(self.detail_code) > 100
            or any(not " " <= character <= "~" for character in self.detail_code)
        ):
            message = "The safe failure detail code is invalid."
            raise ValueError(message)

    def document(self) -> dict[str, int | str | None]:
        """Return the closed redacted evidence document."""
        return {
            "provider_status": self.provider_status,
            "retry_after_ms": self.retry_after_ms,
            "detail_code": self.detail_code,
        }


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    """One normalized provider failure with its exact affected identity."""

    error: TerminalError
    affected_scope_id: str
    evidence: SafeFailureEvidence = SafeFailureEvidence()

    def __post_init__(self) -> None:
        """Require one bounded opaque scope identity."""
        if not isinstance(self.error, TerminalError):
            message = "A failure must contain one terminal error."
            raise TypeError(message)
        if not isinstance(self.evidence, SafeFailureEvidence):
            message = "A failure must contain safe failure evidence."
            raise TypeError(message)
        safe_code = self.error.safe_provider_code
        if safe_code is not None and (
            not safe_code or any(not " " <= character <= "~" for character in safe_code)
        ):
            message = "A safe provider code must use printable ASCII."
            raise ValueError(message)
        if (
            not isinstance(self.affected_scope_id, str)
            or not self.affected_scope_id
            or len(self.affected_scope_id) > _MAXIMUM_IDENTITY_CHARACTERS
        ):
            message = "A failure must contain one bounded affected scope identity."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class AttemptPlan:
    """One immutable adapter-visible attempt snapshot and deadline."""

    claim_id: str
    claim_generation: int
    request_id: str
    request_row_id: str
    service_id: str
    workspace_id: str | None
    attempt_id: str
    attempt_number: int
    candidate_ordinal: int
    assignment_id: str | None
    assignment_revision: str
    route_snapshot_id: str
    route_snapshot_sha256: bytes
    route_configuration_revision: str
    provider_model_route_id: str
    route_generation: int
    provider_instance_id: str
    provider_instance_generation: int
    credential_id: str
    credential_generation: int
    price_version_id: str
    adapter_type: str
    endpoint: str
    wire_model: str
    capabilities: frozenset[str]
    candidate_policy: Mapping[str, object]
    instance_settings: Mapping[str, object]
    route_settings: Mapping[str, object]
    typed_prices: tuple[PriceComponent, ...]
    timeouts: AttemptTimeouts
    logical_deadline: datetime
    attempt_deadline: datetime
    diagnostic: bool
    partial_output: bool
    committed_effect: bool
    started: bool
    dispatched: bool
    recovery_only: bool
    recovery_failure: AttemptFailure | None
    prestart_reservation_id: str | None
    request_terminal: bool

    def __post_init__(self) -> None:
        """Freeze adapter input and enforce all fixed request bounds."""
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or not 1 <= self.attempt_number <= MAXIMUM_PROVIDER_ATTEMPTS
        ):
            message = "The provider attempt number is outside the fixed limit."
            raise ValueError(message)
        if (
            isinstance(self.candidate_ordinal, bool)
            or not isinstance(self.candidate_ordinal, int)
            or not 1 <= self.candidate_ordinal <= MAXIMUM_PROVIDER_ATTEMPTS
        ):
            message = "The assignment candidate ordinal is outside the fixed limit."
            raise ValueError(message)
        generations = (
            self.claim_generation,
            self.route_generation,
            self.provider_instance_generation,
            self.credential_generation,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in generations
        ):
            message = "Route snapshot generations must be positive."
            raise ValueError(message)
        _aware(self.logical_deadline)
        _aware(self.attempt_deadline)
        if self.attempt_deadline > self.logical_deadline:
            message = "An attempt deadline must not exceed the logical deadline."
            raise ValueError(message)
        if not self.typed_prices:
            message = "An eligible provider route must contain typed prices."
            raise ValueError(message)
        prices = tuple(self.typed_prices)
        if any(not isinstance(item, PriceComponent) for item in prices):
            message = "A route snapshot contains an invalid typed price."
            raise TypeError(message)
        object.__setattr__(self, "typed_prices", prices)
        capabilities = frozenset(self.capabilities)
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > _MAXIMUM_SAFE_TEXT_CHARACTERS
            for item in capabilities
        ):
            message = "An adapter capability is invalid."
            raise ValueError(message)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self, "candidate_policy", _freeze_mapping(self.candidate_policy)
        )
        object.__setattr__(
            self, "instance_settings", _freeze_mapping(self.instance_settings)
        )
        object.__setattr__(self, "route_settings", _freeze_mapping(self.route_settings))
        _identities(
            self.claim_id,
            self.request_id,
            self.request_row_id,
            self.service_id,
            self.attempt_id,
            self.assignment_revision,
            self.route_snapshot_id,
            self.route_configuration_revision,
            self.provider_model_route_id,
            self.provider_instance_id,
            self.credential_id,
            self.price_version_id,
            self.adapter_type,
            self.wire_model,
        )
        if (
            not isinstance(self.endpoint, str)
            or not self.endpoint
            or len(self.endpoint) > 2_000
        ):
            message = "A provider endpoint is invalid."
            raise ValueError(message)
        if self.workspace_id is not None:
            _identities(self.workspace_id)
        if self.assignment_id is not None:
            _identities(self.assignment_id)
        if (
            not isinstance(self.route_snapshot_sha256, bytes | bytearray)
            or len(self.route_snapshot_sha256) != 32
        ):
            message = "A route snapshot digest must be SHA-256."
            raise ValueError(message)
        object.__setattr__(
            self, "route_snapshot_sha256", bytes(self.route_snapshot_sha256)
        )
        if not all(
            isinstance(value, bool)
            for value in (
                self.diagnostic,
                self.partial_output,
                self.committed_effect,
                self.started,
                self.dispatched,
                self.recovery_only,
                self.request_terminal,
            )
        ):
            message = "Routing state indicators must be Boolean values."
            raise TypeError(message)
        if self.recovery_failure is not None and not isinstance(
            self.recovery_failure, AttemptFailure
        ):
            message = "A routing recovery failure must contain safe evidence."
            raise TypeError(message)
        if not self.recovery_only and self.recovery_failure is not None:
            message = "A normal provider plan cannot contain a recovery failure."
            raise ValueError(message)
        if self.prestart_reservation_id is not None:
            _identities(self.prestart_reservation_id)
        if self.diagnostic == (self.assignment_id is not None):
            message = "The route target does not match the diagnostic indicator."
            raise ValueError(message)

    @property
    def reservation_key(self) -> str:
        """Use one stable key across claim recovery and budget replay."""
        return self.claim_id


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """One closed provider adapter result."""

    outcome: AttemptOutcome
    failure: AttemptFailure | None = None
    usage: tuple[UsageComponent, ...] = ()

    def __post_init__(self) -> None:
        """Require failure evidence only for a failed outcome."""
        if not isinstance(self.outcome, AttemptOutcome):
            message = "The adapter outcome is invalid."
            raise TypeError(message)
        if self.failure is not None and not isinstance(self.failure, AttemptFailure):
            message = "The adapter failure is invalid."
            raise TypeError(message)
        if (self.outcome is AttemptOutcome.SUCCEEDED) == (self.failure is not None):
            message = "An adapter result failure does not match its outcome."
            raise ValueError(message)
        usage = tuple(self.usage)
        if any(not isinstance(item, UsageComponent) for item in usage):
            message = "Adapter usage contains an invalid component."
            raise TypeError(message)
        if len({item.unit for item in usage}) != len(usage):
            message = "Adapter usage units must be unique."
            raise ValueError(message)
        object.__setattr__(self, "usage", usage)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """One durable candidate budget reservation or denial."""

    permitted: bool
    reservation_id: str | None = None
    failure: AttemptFailure | None = None

    def __post_init__(self) -> None:
        """Require exactly one success or failure shape."""
        if not isinstance(self.permitted, bool):
            message = "The budget permission result must be a Boolean."
            raise TypeError(message)
        if self.reservation_id is not None:
            _identities(self.reservation_id)
        valid_success = (
            self.permitted and self.reservation_id is not None and self.failure is None
        )
        valid_denial = (
            not self.permitted
            and self.reservation_id is None
            and self.failure is not None
        )
        if not (valid_success or valid_denial):
            message = "A budget decision is incomplete."
            raise ValueError(message)


class EligibilityGate(Protocol):
    """Apply the same capability, isolation, policy, and rate controls to all routes."""

    def __call__(self, plan: AttemptPlan) -> AttemptFailure | None:
        """Return a safe failure, or permit the candidate."""
        ...


class BudgetGate(Protocol):
    """Reserve one candidate from the shared logical and owning-scope budgets."""

    def reserve(self, plan: AttemptPlan) -> BudgetDecision:
        """Return one durable reservation or normalized denial."""
        ...

    def release(self, reservation_id: str) -> None:
        """Release a reservation when no provider work started."""
        ...


class AccountingHook(Protocol):
    """Record billable usage for every terminal provider attempt."""

    def __call__(self, plan: AttemptPlan, result: AdapterResult) -> None:
        """Append attempt accounting without changing its routing result."""
        ...


class CompletionHook(Protocol):
    """Apply the terminal provider result to the logical request lifecycle."""

    def __call__(self, plan: AttemptPlan, result: AdapterResult) -> None:
        """Make one non-fallback result terminal through the lifecycle owner."""
        ...


class ProviderAdapter(Protocol):
    """One provider-neutral adapter with explicit execution and stop operations."""

    def execute(self, plan: AttemptPlan, progress: AdapterProgress) -> AdapterResult:
        """Execute one attempt and report each safe operation milestone."""
        ...

    def cancel(self, plan: AttemptPlan) -> AdapterStopEvidence:
        """Request cancellation and return bounded safe evidence."""
        ...


class AdapterProgress(Protocol):
    """Report provider operation milestones without provider content."""

    def __call__(self, phase: AdapterPhase) -> None:
        """Report one ordered connect, first-byte, or progress milestone."""
        ...


def fixed_deadlines(
    *, admitted_at: datetime, now: datetime, configured_execution_ms: int
) -> tuple[AttemptTimeouts, datetime, datetime]:
    """Shorten one late attempt so all work stays inside 15 minutes."""
    _aware(admitted_at)
    _aware(now)
    if (
        isinstance(configured_execution_ms, bool)
        or not isinstance(configured_execution_ms, int)
        or not 100 <= configured_execution_ms <= MAXIMUM_ATTEMPT_MILLISECONDS
    ):
        message = "The configured attempt timeout is outside the accepted range."
        raise ValueError(message)
    logical_deadline = admitted_at + timedelta(
        milliseconds=MAXIMUM_LOGICAL_MILLISECONDS
    )
    remaining_ms = int((logical_deadline - now).total_seconds() * 1_000)
    execution_ms = min(configured_execution_ms, remaining_ms)
    if execution_ms < 100:
        message = "There is not enough logical time for useful provider work."
        raise ValueError(message)
    timeouts = AttemptTimeouts.fixed_for_execution(execution_ms)
    return timeouts, logical_deadline, now + timedelta(milliseconds=execution_ms)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        message = "Routing times must include a time zone."
        raise ValueError(message)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if any(not isinstance(key, str) for key in value):
        message = "Adapter setting object keys must be strings."
        raise TypeError(message)
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and abs(value) <= Decimal("1e38")
    ):
        return value
    if isinstance(value, float) and math.isfinite(value) and abs(value) <= 1e38:
        return value
    message = "Adapter settings must contain closed JSON values."
    raise TypeError(message)


def _identities(*values: str) -> None:
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > _MAXIMUM_IDENTITY_CHARACTERS
        for value in values
    ):
        message = "A routing identity is invalid."
        raise ValueError(message)

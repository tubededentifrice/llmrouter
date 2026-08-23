"""Exact hierarchical budget values."""
# ruff: noqa: D105

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from llmrouter_backend.accounting.model import currency_code, exact_decimal

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal


class BudgetScopeKind(StrEnum):
    """Supported hard-budget scopes."""

    GLOBAL = "global"
    SERVICE = "service"
    WORKSPACE = "workspace"
    ASSIGNMENT = "assignment"
    HOST_CEILING = "host_ceiling"
    LOGICAL_REQUEST = "logical_request"


class ResetPeriod(StrEnum):
    """Supported hard-limit reset periods."""

    NONE = "none"
    DAILY = "daily"
    MONTHLY = "monthly"


class EnforcementState(StrEnum):
    """Safe effective enforcement states."""

    AVAILABLE = "available"
    WARNING = "warning"
    EXHAUSTED = "exhausted"


class ReservationState(StrEnum):
    """Safe candidate reservation results."""

    RESERVED = "reserved"
    SKIPPED = "skipped"
    EXHAUSTED = "exhausted"


class BudgetCandidateKind(StrEnum):
    """Closed candidate kinds that can produce billable work."""

    PROVIDER_ROUTE = "provider_route"
    EXTERNAL_TOOL = "external_tool"
    BUSINESS_TOOL = "business_tool"


@dataclass(frozen=True, slots=True)
class Money:
    """One exact nonnegative currency amount."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", exact_decimal(self.amount))
        object.__setattr__(self, "currency", currency_code(self.currency))


@dataclass(frozen=True, slots=True)
class SignedMoney:
    """One exact signed currency amount."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", exact_decimal(self.amount, signed=True))
        object.__setattr__(self, "currency", currency_code(self.currency))


@dataclass(frozen=True, slots=True)
class BudgetTarget:
    """One global, service, workspace, or assignment limit target."""

    kind: BudgetScopeKind
    service_id: str | None = None
    workspace_id: str | None = None
    assignment_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is BudgetScopeKind.GLOBAL:
            valid = not any((self.service_id, self.workspace_id, self.assignment_id))
        elif self.kind is BudgetScopeKind.SERVICE:
            valid = bool(self.service_id) and not any(
                (self.workspace_id, self.assignment_id)
            )
        elif self.kind is BudgetScopeKind.WORKSPACE:
            valid = (
                bool(self.service_id and self.workspace_id) and not self.assignment_id
            )
        elif self.kind is BudgetScopeKind.ASSIGNMENT:
            valid = bool(self.service_id and self.assignment_id)
        else:
            valid = False
        if not valid:
            msg = "The budget target does not match its scope kind."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    """One current hard limit and warning threshold."""

    scope_id: str
    target: BudgetTarget
    hard_limit: Money
    warning_threshold: Money | None
    reset_period: ResetPeriod
    revision: str
    effective_at: datetime

    def __post_init__(self) -> None:
        if not self.scope_id or not self.revision:
            msg = "Budget limit identities must not be empty."
            raise ValueError(msg)
        if self.warning_threshold is not None:
            if self.warning_threshold.currency != self.hard_limit.currency:
                msg = "The warning threshold currency must match the limit."
                raise ValueError(msg)
            if self.warning_threshold.amount > self.hard_limit.amount:
                msg = "The warning threshold must not exceed the hard limit."
                raise ValueError(msg)
        _aware(self.effective_at)


@dataclass(frozen=True, slots=True)
class HostCeiling:
    """One host-owned, non-bypassable workspace ceiling."""

    service_id: str
    workspace_id: str
    amount: Money
    revision: str
    effective_at: datetime
    operation_id: str

    def __post_init__(self) -> None:
        if not all(
            (self.service_id, self.workspace_id, self.revision, self.operation_id)
        ):
            msg = "Host ceiling identities must not be empty."
            raise ValueError(msg)
        _aware(self.effective_at)


@dataclass(frozen=True, slots=True)
class EnforcementSummary:
    """One safe budget enforcement summary."""

    scope_kind: BudgetScopeKind
    limit: Money
    reserved: Money
    used: Money
    corrected: SignedMoney
    remaining: Money
    state: EnforcementState
    revision: str
    warning_threshold: Money | None
    reset_period: ResetPeriod


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """One atomic candidate reservation or safe skip result."""

    state: ReservationState
    request_row_id: str
    candidate_id: str
    currency: str
    reservation_id: str | None = None
    replayed: bool = False
    rejected_scope: BudgetScopeKind | None = None
    summaries: tuple[EnforcementSummary, ...] = ()
    accounting_scope_id: str | None = None

    @property
    def external_effects_permitted(self) -> bool:
        """Permit work only after a new durable reservation exists."""
        return (
            self.state is ReservationState.RESERVED
            and self.reservation_id is not None
            and not self.replayed
        )


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "Budget times must include a time zone."
        raise ValueError(msg)

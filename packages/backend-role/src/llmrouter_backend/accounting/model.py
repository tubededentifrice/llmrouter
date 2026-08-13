"""Exact accounting and immutable price synchronization values."""
# ruff: noqa: D105, EM101, PLR2004, S105, SIM101, TC003, TRY003

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from llmrouter_backend.spool import CanonicalEvent, EventClass

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_DECIMAL = Decimal("99999999999999999999.999999999999999999")
MAX_SNAPSHOT_TEXT_CHARACTERS = 10_000_000
_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,99}$")


def exact_decimal(value: str | Decimal, *, signed: bool = False) -> Decimal:
    """Return one finite fixed decimal value without binary float input."""
    if isinstance(value, float) or isinstance(value, bool):
        raise TypeError("Accounting values must not use binary floating point.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("The accounting decimal value is malformed.") from error
    if not result.is_finite() or abs(result) > MAX_DECIMAL:
        raise ValueError("The accounting decimal value is outside its safe range.")
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -18:
        raise ValueError("The accounting decimal value has too many fraction digits.")
    if not signed and result < 0:
        raise ValueError("The accounting decimal value must not be negative.")
    return result


def currency_code(value: str) -> str:
    """Require one exact ISO-style accounting currency code."""
    if len(value) != 3 or not value.isascii() or not value.isupper():
        raise ValueError("The accounting currency must have three uppercase letters.")
    return value


class UsageUnit(StrEnum):
    """Typed provider and tool usage units."""

    INPUT_TOKEN = "input_token"  # nosec B105 - This is a usage unit.
    OUTPUT_TOKEN = "output_token"  # nosec B105 - This is a usage unit.
    CACHED_TOKEN = "cached_token"  # nosec B105 - This is a usage unit.
    REQUEST = "request"
    IMAGE = "image"
    AUDIO_SECOND = "audio_second"
    SEARCH = "search"
    TOOL_UNIT = "tool_unit"
    OTHER = "other"


class AccountingSubjectKind(StrEnum):
    """Stable identities that can produce accounting."""

    LOGICAL_REQUEST = "logical_request"
    PROVIDER_ATTEMPT = "provider_attempt"
    EXTERNAL_TOOL_ATTEMPT = "external_tool_attempt"
    BUSINESS_TOOL_CALL = "business_tool_call"


class AttemptOutcome(StrEnum):
    """Outcomes that retain reported usage."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"
    UNCERTAIN = "uncertain"


class CorrectionKind(StrEnum):
    """Append-only accounting correction sources."""

    PRICE = "price"
    PROVIDER_USAGE = "provider_usage"
    INVOICE = "invoice"


class SynchronizationState(StrEnum):
    """Safe price synchronization states."""

    MANUAL = "manual"
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    FAILED = "failed"


class SynchronizationStatus(StrEnum):
    """One row result in a bounded synchronization."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    MISSING = "missing"
    FAILED = "failed"


class SynchronizationRunState(StrEnum):
    """Durable state for one price synchronization operation."""

    PREVIEWED = "previewed"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UsageComponent:
    """One exact typed usage quantity."""

    unit: UsageUnit
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", exact_decimal(self.quantity))


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """One exact signed usage correction or aggregate."""

    unit: UsageUnit
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, signed=True))


@dataclass(frozen=True, slots=True)
class PriceComponent:
    """One exact price for a positive unit quantity."""

    unit: UsageUnit
    price: Decimal
    currency: str
    raw_source_value: str
    unit_quantity: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", exact_decimal(self.price))
        quantity = exact_decimal(self.unit_quantity)
        if quantity == 0:
            raise ValueError("A price unit quantity must be positive.")
        object.__setattr__(self, "unit_quantity", quantity)
        object.__setattr__(self, "currency", currency_code(self.currency))
        if not self.raw_source_value or len(self.raw_source_value) > 200:
            raise ValueError("A price must keep one bounded raw source value.")


@dataclass(frozen=True, slots=True)
class RawPriceComponent:
    """One bounded untrusted price value from a source snapshot."""

    unit: str
    price: str
    currency: str
    raw_source_value: str
    unit_quantity: str = "1"

    def __post_init__(self) -> None:
        if any(
            len(value) > 200
            for value in (
                self.unit,
                self.price,
                self.currency,
                self.raw_source_value,
                self.unit_quantity,
            )
        ):
            raise ValueError("A raw price source value is too large.")


@dataclass(frozen=True, slots=True)
class AccountingEvent:
    """One immutable canonical usage and cost fact."""

    event_id: str
    canonical_event_id: str
    request_row_id: str
    service_id: str
    workspace_id: str | None
    budget_scope_id: str
    subject_kind: AccountingSubjectKind
    subject_id: str
    outcome: AttemptOutcome
    currency: str
    usage: tuple[UsageComponent, ...]
    occurred_at: datetime
    price_version_id: str | None = None
    reported_amount: Decimal | None = None
    assignment_id: str | None = None
    budget_ledger_event_id: str | None = None

    def __post_init__(self) -> None:
        if len({item.unit for item in self.usage}) != len(self.usage):
            raise ValueError("Accounting usage units must be unique.")
        object.__setattr__(
            self, "usage", tuple(sorted(self.usage, key=lambda item: item.unit.value))
        )
        for value in (
            self.event_id,
            self.canonical_event_id,
            self.request_row_id,
            self.service_id,
            self.budget_scope_id,
            self.subject_id,
        ):
            if not value:
                raise ValueError("Accounting identities must be complete.")
        object.__setattr__(self, "currency", currency_code(self.currency))
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Accounting event time must include a time zone.")
        if self.reported_amount is not None:
            object.__setattr__(
                self, "reported_amount", exact_decimal(self.reported_amount)
            )
        if self.price_version_id is not None and self.reported_amount is not None:
            raise ValueError("A priced event must not also supply a reported amount.")
        if (
            self.price_version_id is not None
            and self.subject_kind is not AccountingSubjectKind.PROVIDER_ATTEMPT
        ):
            raise ValueError("Only a provider attempt can use a route price version.")
        if self.assignment_id is not None and (
            not self.assignment_id
            or self.subject_kind is not AccountingSubjectKind.PROVIDER_ATTEMPT
        ):
            raise ValueError("Only a provider attempt can name an assignment.")
        if self.budget_ledger_event_id == "":
            raise ValueError("A budget ledger event identity must not be empty.")

    def canonical_payload(self) -> bytes:
        """Return the exact payload that the canonical ledger must protect."""
        value = {
            "budget_scope_id": self.budget_scope_id,
            "budget_ledger_event_id": self.budget_ledger_event_id,
            "assignment_id": self.assignment_id,
            "currency": self.currency,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at.isoformat(),
            "outcome": self.outcome.value,
            "price_version_id": self.price_version_id,
            "reported_amount": None
            if self.reported_amount is None
            else str(self.reported_amount),
            "request_row_id": self.request_row_id,
            "service_id": self.service_id,
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind.value,
            "usage": [
                {"quantity": str(item.quantity), "unit": item.unit.value}
                for item in sorted(self.usage, key=lambda item: item.unit.value)
            ],
            "workspace_id": self.workspace_id,
        }
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()

    def canonical_payload_sha256(self) -> bytes:
        """Return the canonical payload digest for central-ledger binding."""
        return hashlib.sha256(self.canonical_payload()).digest()

    def canonical_event(
        self, source_node_id: str, source_sequence: int
    ) -> CanonicalEvent:
        """Build the exact D01 canonical envelope for this accounting fact."""
        return CanonicalEvent(
            self.canonical_event_id,
            source_node_id,
            source_sequence,
            EventClass.ACCOUNTING,
            self.canonical_payload(),
            self.occurred_at,
        )


@dataclass(frozen=True, slots=True)
class AccountingCorrection:
    """One immutable signed correction to an original event."""

    correction_id: str
    source_event_id: str
    kind: CorrectionKind
    currency: str
    amount_delta: Decimal
    usage_delta: tuple[UsageDelta, ...]
    source: str
    reason: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if len({item.unit for item in self.usage_delta}) != len(self.usage_delta):
            raise ValueError("Correction usage units must be unique.")
        object.__setattr__(
            self,
            "usage_delta",
            tuple(sorted(self.usage_delta, key=lambda item: item.unit.value)),
        )
        object.__setattr__(self, "currency", currency_code(self.currency))
        object.__setattr__(
            self, "amount_delta", exact_decimal(self.amount_delta, signed=True)
        )
        if not self.correction_id or not self.source_event_id:
            raise ValueError("Correction identities must be complete.")
        if not self.source or not self.reason:
            raise ValueError("A correction must identify its source and reason.")
        if len(self.source) > 100 or len(self.reason) > 500:
            raise ValueError("Correction source and reason values must be bounded.")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Correction time must include a time zone.")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One immutable upstream price source snapshot."""

    source_name: str
    fetched_at: datetime
    content_sha256: str
    rows: Mapping[str, tuple[PriceComponent | RawPriceComponent, ...]]
    source_revision: str | None = None
    http_validator: str | None = None
    source_available: bool = True

    def __post_init__(self) -> None:
        self._validate_rows(self.rows)
        frozen_rows = {key: tuple(value) for key, value in self.rows.items()}
        object.__setattr__(self, "rows", MappingProxyType(frozen_rows))
        if (
            not _SOURCE_NAME.fullmatch(self.source_name)
            or len(self.content_sha256) != 64
        ):
            raise ValueError("A price snapshot identity is invalid.")
        if self.source_revision is not None and len(self.source_revision) > 500:
            raise ValueError("A price source revision is too large.")
        if self.http_validator is not None and len(self.http_validator) > 500:
            raise ValueError("A price source validator is too large.")
        try:
            bytes.fromhex(self.content_sha256)
        except ValueError as error:
            raise ValueError(
                "A price snapshot digest must be lowercase SHA-256."
            ) from error
        if self.content_sha256.lower() != self.content_sha256:
            raise ValueError("A price snapshot digest must be lowercase SHA-256.")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("A price snapshot time must include a time zone.")
        if hashlib.sha256(self.canonical_content()).hexdigest() != self.content_sha256:
            raise ValueError("A price snapshot digest does not match its rows.")

    def canonical_content(self) -> bytes:
        """Return deterministic bounded source content for digest verification."""
        rows = {
            key: [
                {
                    "currency": item.currency,
                    "price": str(item.price),
                    "raw_source_value": item.raw_source_value,
                    "unit": item.unit.value
                    if isinstance(item, PriceComponent)
                    else item.unit,
                    "unit_quantity": str(item.unit_quantity),
                }
                for item in value
            ]
            for key, value in sorted(self.rows.items())
        }
        return json.dumps(
            {"rows": rows, "source_available": self.source_available},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()

    @staticmethod
    def digest(
        rows: Mapping[str, tuple[PriceComponent | RawPriceComponent, ...]],
        *,
        source_available: bool = True,
    ) -> str:
        """Calculate the required digest for source rows."""
        SourceSnapshot._validate_rows(rows)
        temporary = object.__new__(SourceSnapshot)
        object.__setattr__(temporary, "rows", rows)
        object.__setattr__(temporary, "source_available", source_available)
        return hashlib.sha256(temporary.canonical_content()).hexdigest()

    @staticmethod
    def _validate_rows(
        rows: Mapping[str, tuple[PriceComponent | RawPriceComponent, ...]],
    ) -> None:
        if len(rows) > 10_000 or any(
            not key or len(key) > 500 or len(value) > len(UsageUnit)
            for key, value in rows.items()
        ):
            raise ValueError("A price snapshot exceeds its safe row bounds.")
        text_characters = sum(
            len(key)
            + sum(
                len(str(item.unit))
                + len(str(item.price))
                + len(item.currency)
                + len(item.raw_source_value)
                + len(str(item.unit_quantity))
                for item in value
            )
            for key, value in rows.items()
        )
        if text_characters > MAX_SNAPSHOT_TEXT_CHARACTERS:
            raise ValueError("A price snapshot exceeds its safe content bounds.")


@dataclass(frozen=True, slots=True)
class SynchronizationRow:
    """One safe result from price synchronization."""

    provider_model_route_id: str
    source_name: str
    lookup_identifier: str
    old_prices: tuple[PriceComponent, ...]
    new_prices: tuple[PriceComponent, ...]
    status: SynchronizationStatus
    synchronization_state: SynchronizationState
    synchronized_at: datetime
    price_version_id: str | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class SourceSnapshotEvidence:
    """Safe immutable evidence for one consumed source snapshot."""

    source_name: str
    fetched_at: datetime
    content_sha256: str
    source_revision: str | None = None
    http_validator: str | None = None


@dataclass(frozen=True, slots=True)
class SynchronizationResult:
    """One dry-run or committed price synchronization result."""

    operation_id: str
    dry_run: bool
    source_snapshot_id: str
    rows: tuple[SynchronizationRow, ...]
    resulting_configuration_revision: str | None = None
    resulting_configuration_revisions: tuple[str, ...] = ()
    state: SynchronizationRunState = SynchronizationRunState.COMPLETED
    source_snapshot: SourceSnapshotEvidence | None = None


@dataclass(frozen=True, slots=True)
class AccountingSummary:
    """One bounded exact scoped accounting result."""

    currency: str
    logical_requests: int
    attempts: int
    usage: tuple[UsageComponent, ...]
    cost: Decimal
    corrections: Decimal

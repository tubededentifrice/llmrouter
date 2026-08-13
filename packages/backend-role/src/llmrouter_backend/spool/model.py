"""Configured values for the local canonical-event spool."""
# ruff: noqa: TC003

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

_MINIMUM_FRAME_OVERHEAD = 256
_MINIMUM_RESERVATION_OVERHEAD = 512
_UUID_TEXT_BYTES = 36


class PressureState(StrEnum):
    """Operator-visible spool pressure states."""

    NORMAL = "normal"
    WARNING = "warning"
    SHEDDING = "shedding"
    STOPPED = "stopped"
    EMERGENCY = "emergency"


class WorkClass(StrEnum):
    """Admission classes that have different pressure rules."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"
    BATCH = "batch"
    PLAYGROUND = "playground"
    AGENT = "agent"
    CANCELLATION = "cancellation"
    RECONCILIATION = "reconciliation"
    SECURITY = "security"


class EventClass(StrEnum):
    """Canonical event classes accepted by the central ledger."""

    ACCOUNTING = "accounting"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class SpoolLimits:
    """Deployment-supplied spool limits, with no product defaults."""

    capacity_bytes: int
    warning_bytes: int
    shedding_bytes: int
    stop_bytes: int
    emergency_reserve_bytes: int
    recovery_hysteresis_bytes: int
    operational_headroom_bytes: int

    def __post_init__(self) -> None:
        """Reject limits that can consume the emergency reserve."""
        values = (
            self.capacity_bytes,
            self.warning_bytes,
            self.shedding_bytes,
            self.stop_bytes,
            self.emergency_reserve_bytes,
            self.recovery_hysteresis_bytes,
            self.operational_headroom_bytes,
        )
        if any(value <= 0 for value in values):
            msg = "All spool limits must be positive."
            raise ValueError(msg)
        usable = self.capacity_bytes - self.emergency_reserve_bytes
        if not 0 < self.warning_bytes < self.shedding_bytes < self.stop_bytes < usable:
            msg = "Spool thresholds must be ordered below the emergency reserve."
            raise ValueError(msg)
        if self.recovery_hysteresis_bytes >= self.warning_bytes:
            msg = "Spool recovery hysteresis must be less than the warning threshold."
            raise ValueError(msg)
        if self.operational_headroom_bytes < self.capacity_bytes:
            msg = "Spool operational headroom must cover one complete compaction."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CanonicalLoadBounds:
    """Validated fixed inputs for one maximum canonical event load."""

    maximum_event_bytes: int
    encrypted_frame_overhead_bytes: int
    reservation_state_overhead_bytes: int
    fixed_event_count: int
    maximum_provider_attempts: int
    events_per_provider_attempt: int
    maximum_tool_steps: int
    events_per_tool_step: int
    maximum_identity_bytes: int
    maximum_receipt_bytes: int

    def __post_init__(self) -> None:
        """Require explicit finite values."""
        if self.maximum_event_bytes <= 0 or self.fixed_event_count <= 0:
            msg = "Event size and fixed event count must be positive."
            raise ValueError(msg)
        counts = (
            self.encrypted_frame_overhead_bytes,
            self.reservation_state_overhead_bytes,
            self.maximum_provider_attempts,
            self.events_per_provider_attempt,
            self.maximum_tool_steps,
            self.events_per_tool_step,
        )
        if any(value < 0 for value in counts):
            msg = "Canonical event counts must not be negative."
            raise ValueError(msg)
        if (
            self.encrypted_frame_overhead_bytes < _MINIMUM_FRAME_OVERHEAD
            or self.reservation_state_overhead_bytes < _MINIMUM_RESERVATION_OVERHEAD
        ):
            msg = "Canonical spool frame overhead must cover the fixed codec envelope."
            raise ValueError(msg)
        if (
            self.maximum_identity_bytes < _UUID_TEXT_BYTES
            or self.maximum_receipt_bytes <= 0
        ):
            msg = "Canonical spool identity and receipt bounds are too small."
            raise ValueError(msg)

    @property
    def maximum_event_count(self) -> int:
        """Calculate the maximum event count for one admitted unit of work."""
        return (
            self.fixed_event_count
            + self.maximum_provider_attempts * self.events_per_provider_attempt
            + self.maximum_tool_steps * self.events_per_tool_step
        )

    @property
    def maximum_load_bytes(self) -> int:
        """Calculate the maximum encoded canonical event load."""
        encoded_payload_bytes = 4 * ((self.maximum_event_bytes + 2) // 3)
        return self.reservation_state_overhead_bytes + self.maximum_event_count * (
            encoded_payload_bytes + self.encrypted_frame_overhead_bytes
        )

    @property
    def maximum_canonical_event_count(self) -> int:
        """Include one possible mandatory pressure-policy audit event."""
        return self.maximum_event_count + 1

    @property
    def maximum_reserved_load_bytes(self) -> int:
        """Reserve bounded work plus one possible pressure-policy audit event."""
        encoded_payload_bytes = 4 * ((self.maximum_event_bytes + 2) // 3)
        event_and_release = (
            encoded_payload_bytes
            + self.maximum_identity_bytes * 3
            + self.maximum_receipt_bytes
            + self.encrypted_frame_overhead_bytes * 2
        )
        return (
            self.reservation_state_overhead_bytes
            + self.maximum_canonical_event_count * event_and_release
            + self.encrypted_frame_overhead_bytes
        )

    def required_node_capacity(self, maximum_admitted_concurrency: int) -> int:
        """Calculate the node load for an explicit concurrency limit."""
        if maximum_admitted_concurrency <= 0:
            msg = "Maximum admitted concurrency must be positive."
            raise ValueError(msg)
        return self.maximum_reserved_load_bytes * maximum_admitted_concurrency


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """One immutable canonical accounting or audit event."""

    event_id: str
    source_node_id: str
    source_sequence: int
    event_class: EventClass
    payload: bytes
    occurred_at: datetime

    def __post_init__(self) -> None:
        """Reject incomplete event identities and unsafe times."""
        if not self.event_id or not self.source_node_id or self.source_sequence <= 0:
            msg = "Canonical event identity must be complete."
            raise ValueError(msg)
        try:
            event_id = UUID(self.event_id)
            source_node_id = UUID(self.source_node_id)
        except ValueError as error:
            msg = "Canonical event identities must be UUID values."
            raise ValueError(msg) from error
        if str(event_id) != self.event_id or str(source_node_id) != self.source_node_id:
            msg = "Canonical event identities must use canonical UUID text."
            raise ValueError(msg)
        if not self.payload:
            msg = "Canonical event payload must not be empty."
            raise ValueError(msg)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            msg = "Canonical event time must include a time zone."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """The durable local result of one spool reservation."""

    reservation_id: str
    reserved_bytes: int
    capture_enabled: bool
    capture_reason: str
    diagnostic_logs_enabled: bool
    pressure_state: PressureState
    delivery_urgent: bool
    operator_alert: bool


@dataclass(frozen=True, slots=True)
class SpoolHealth:
    """Safe internal health data for operators."""

    state: PressureState
    used_bytes: int
    capacity_bytes: int
    oldest_event_age_seconds: int | None
    shed_classes: tuple[WorkClass, ...]
    last_delivery_error: str | None
    estimated_remaining_bytes: int
    delivery_urgent: bool
    operator_alert: bool
    external_effects_allowed: bool
    optional_work_allowed: bool

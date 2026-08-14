"""Closed execution lifecycle values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import RFC_4122, UUID

if TYPE_CHECKING:
    from datetime import datetime

TERMINAL_STATES = frozenset(
    {
        "succeeded",
        "failed",
        "interrupted",
        "cancelled",
        "uncertain",
    }
)
CANCELLATION_RECONCILIATION_SECONDS = 600
STREAM_REPLAY_AFTER_TERMINAL_SECONDS = 900
MAXIMUM_TERMINAL_MESSAGE_CHARACTERS = 1000
MAXIMUM_PROVIDER_CODE_CHARACTERS = 200
MAXIMUM_OPERATION_ID_CHARACTERS = 500
MAXIMUM_STOP_CODE_CHARACTERS = 100
UUID_VERSION_SEVEN = 7
_UUID_V7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ExecutionKind(StrEnum):
    """Execution records that share lifecycle rules."""

    MODEL = "model"
    SHARED_TOOL = "shared_tool"
    AGENT_RUN = "agent_run"


class ExecutionState(StrEnum):
    """Product-neutral request and run states."""

    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class TerminalErrorClass(StrEnum):
    """Closed safe terminal error classes."""

    AUTHENTICATION = "authentication"
    POLICY = "policy"
    BUDGET = "budget"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    INCOMPATIBLE_REQUEST = "incompatible_request"
    CANCELLED = "cancelled"
    UNCERTAIN_EFFECT = "uncertain_effect"
    ROUTER_INTERNAL = "router_internal"


class ErrorScope(StrEnum):
    """Closed known failure scopes."""

    ATTEMPT = "attempt"
    PROVIDER_MODEL_ROUTE = "provider_model_route"
    PROVIDER_INSTANCE = "provider_instance"
    CREDENTIAL = "credential"
    ASSIGNMENT_CANDIDATE = "assignment_candidate"
    LOGICAL_REQUEST = "logical_request"


@dataclass(frozen=True, slots=True)
class TerminalError:
    """One bounded safe error that matches the public contract."""

    error_class: TerminalErrorClass
    affected_scope: ErrorScope
    message: str
    safe_provider_code: str | None = None

    def __post_init__(self) -> None:
        """Reject values outside the closed safe error bounds."""
        if len(self.message) > MAXIMUM_TERMINAL_MESSAGE_CHARACTERS:
            message = "A safe terminal message is too large."
            raise ValueError(message)
        if (
            self.safe_provider_code is not None
            and len(self.safe_provider_code) > MAXIMUM_PROVIDER_CODE_CHARACTERS
        ):
            message = "A safe provider code is too large."
            raise ValueError(message)

    def document(self) -> dict[str, str]:
        """Return the closed public JSON object."""
        result = {
            "class": self.error_class.value,
            "affected_scope": self.affected_scope.value,
            "message": self.message,
        }
        if self.safe_provider_code is not None:
            result["safe_provider_code"] = self.safe_provider_code
        return result

    @classmethod
    def from_document(cls, value: object) -> TerminalError | None:
        """Validate one stored safe error."""
        if value is None:
            return None
        required = {
            "class",
            "affected_scope",
            "message",
        }
        optional = {"safe_provider_code"}
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or not value.keys() <= required | optional
            or not isinstance(value.get("class"), str)
            or not isinstance(value.get("affected_scope"), str)
            or not isinstance(value.get("message"), str)
            or (
                value.get("safe_provider_code") is not None
                and not isinstance(value.get("safe_provider_code"), str)
            )
        ):
            message = "A stored terminal error is invalid."
            raise ValueError(message)
        return cls(
            TerminalErrorClass(value["class"]),
            ErrorScope(value["affected_scope"]),
            value["message"],
            None
            if value.get("safe_provider_code") is None
            else value["safe_provider_code"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """One scoped public execution identity."""

    kind: ExecutionKind
    public_id: str

    def __post_init__(self) -> None:
        """Require one nonzero canonical UUID public identity."""
        try:
            identity = UUID(self.public_id)
        except (TypeError, ValueError) as error:
            message = "An execution target identity must be a UUID."
            raise ValueError(message) from error
        if (
            _UUID_V7.fullmatch(self.public_id) is None
            or identity.version != UUID_VERSION_SEVEN
            or identity.variant != RFC_4122
            or (
                ((identity.int >> 64) & 0xFFF) == 0
                and (identity.int & ((1 << 62) - 1)) == 0
            )
        ):
            message = "An execution target identity must be a canonical UUIDv7."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ExecutionAdmission:
    """The durable identity and links returned at execution admission."""

    request_id: str
    run_id: str | None
    admitted_at: datetime
    state: ExecutionState
    state_revision: int
    status_url: str
    cancel_url: str
    events_url: str | None
    fingerprint_version: str
    capture_enabled: bool
    capture_reason: str


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    """The lifecycle fields required by a status response."""

    target: ExecutionTarget
    state: ExecutionState
    state_revision: int
    admitted_at: datetime
    last_transition_at: datetime
    terminal_at: datetime | None
    safe_error: TerminalError | None
    partial_output: bool
    committed_effects: bool
    configuration_revision: str
    admission: ExecutionAdmission
    status_url: str
    cancel_url: str
    events_url: str | None
    owner_epoch: int | None = None

    @property
    def terminal(self) -> bool:
        """Return true only for an immutable terminal state."""
        return self.state in TERMINAL_STATES

    @property
    def fallback_permitted(self) -> bool:
        """Permit fallback only before cancellation or a commit boundary."""
        return (
            not self.terminal
            and self.state is not ExecutionState.CANCEL_REQUESTED
            and not self.partial_output
            and not self.committed_effects
        )

    @property
    def accepts_late_usage(self) -> bool:
        """Late accounting does not change lifecycle state."""
        return True


@dataclass(frozen=True, slots=True)
class AdapterStopEvidence:
    """Safe evidence from one active adapter operation."""

    operation_id: str
    supported: bool
    stop_requested: bool
    confirmed_stopped: bool
    safe_code: str | None = None

    def __post_init__(self) -> None:
        """Reject unsafe or internally inconsistent stop evidence."""
        if not all(
            isinstance(value, bool)
            for value in (self.supported, self.stop_requested, self.confirmed_stopped)
        ):
            message = "Adapter stop evidence flags must be booleans."
            raise TypeError(message)
        if (
            not self.operation_id
            or len(self.operation_id) > MAXIMUM_OPERATION_ID_CHARACTERS
        ):
            message = "The adapter operation identity is invalid."
            raise ValueError(message)
        if (
            self.safe_code is not None
            and len(self.safe_code) > MAXIMUM_STOP_CODE_CHARACTERS
        ):
            message = "The adapter stop code is too large."
            raise ValueError(message)
        if self.confirmed_stopped and not self.stop_requested:
            message = "Stop confirmation needs a stop request."
            raise ValueError(message)
        if self.confirmed_stopped and not self.supported:
            message = "An unsupported stop operation cannot be confirmed."
            raise ValueError(message)

    def document(self) -> dict[str, object]:
        """Return one bounded safe audit document."""
        return {
            "operation_id": self.operation_id,
            "supported": self.supported,
            "stop_requested": self.stop_requested,
            "confirmed_stopped": self.confirmed_stopped,
            "safe_code": self.safe_code,
        }


class AdapterStop(Protocol):
    """One adapter stop operation called after durable cancel intent."""

    def __call__(self) -> AdapterStopEvidence:
        """Request a stop and return safe evidence."""
        ...


@dataclass(frozen=True, slots=True)
class CancellationResult:
    """The current result of one idempotent cancel operation."""

    status: ExecutionStatus
    too_late: bool
    reconcile_deadline: datetime | None
    evidence: tuple[AdapterStopEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class RunLease:
    """One fenced run owner lease."""

    run_id: str
    owner_node_id: str
    control_epoch: int
    owner_epoch: int
    lease_generation: int
    expires_at: datetime

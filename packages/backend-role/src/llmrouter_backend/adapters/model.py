"""Provider-neutral bounded values for model adapter input and output."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llmrouter_backend.credential_store import SecretLease
    from llmrouter_backend.routing import AttemptPlan

MAXIMUM_MESSAGE_CHARACTERS = 1_048_576
MAXIMUM_MESSAGES = 1_000
MAXIMUM_REQUEST_TEXT_BYTES = 8_388_608
MAXIMUM_OUTPUT_UNITS = 1_000_000
MAXIMUM_OUTPUT_DELTA_BYTES = 262_144


class ModelOperation(StrEnum):
    """Closed text operations that the first model adapter accepts."""

    COMPLETE = "chat.complete"
    STREAM = "chat.stream"


class MessageRole(StrEnum):
    """Provider-neutral roles for one text message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelOutputEventKind(StrEnum):
    """Closed provider-neutral output events from one adapter attempt."""

    DELTA = "text.delta"
    COMPLETED = "text.completed"


@dataclass(frozen=True, slots=True, repr=False)
class ModelMessage:
    """One bounded provider-neutral text message."""

    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        """Reject unsupported roles and unbounded text."""
        if not isinstance(self.role, MessageRole):
            message = "A model message role is invalid."
            raise TypeError(message)
        if (
            not isinstance(self.content, str)
            or not 1 <= len(self.content) <= MAXIMUM_MESSAGE_CHARACTERS
        ):
            message = "A model message must contain bounded text."
            raise ValueError(message)

    def __repr__(self) -> str:
        """Do not put model input in a diagnostic representation."""
        return f"ModelMessage(role={self.role!r}, content=[REDACTED])"


@dataclass(frozen=True, slots=True, repr=False)
class ModelAdapterRequest:
    """One closed provider-neutral request supplied after admission."""

    operation: ModelOperation
    messages: tuple[ModelMessage, ...]
    max_output_units: int
    temperature: Decimal | None = None

    def __post_init__(self) -> None:
        """Freeze and bound all provider-visible request controls."""
        if not isinstance(self.operation, ModelOperation):
            message = "A model adapter operation is invalid."
            raise TypeError(message)
        messages = tuple(self.messages)
        if not 1 <= len(messages) <= MAXIMUM_MESSAGES or any(
            not isinstance(item, ModelMessage) for item in messages
        ):
            message = "A model adapter request has invalid messages."
            raise ValueError(message)
        try:
            request_text_bytes = sum(
                len(item.content.encode("utf-8")) for item in messages
            )
        except UnicodeEncodeError as error:
            message = "A model adapter request contains invalid Unicode."
            raise ValueError(message) from error
        if request_text_bytes > MAXIMUM_REQUEST_TEXT_BYTES:
            message = "A model adapter request contains too much text."
            raise ValueError(message)
        object.__setattr__(self, "messages", messages)
        if (
            isinstance(self.max_output_units, bool)
            or not isinstance(self.max_output_units, int)
            or not 1 <= self.max_output_units <= MAXIMUM_OUTPUT_UNITS
        ):
            message = "A model output limit is outside the accepted range."
            raise ValueError(message)
        if self.temperature is not None and (
            not isinstance(self.temperature, Decimal)
            or not self.temperature.is_finite()
            or not Decimal(0) <= self.temperature <= Decimal(2)
        ):
            message = "A model temperature is outside the accepted range."
            raise ValueError(message)

    def __repr__(self) -> str:
        """Do not put model input in a diagnostic representation."""
        return (
            "ModelAdapterRequest("
            f"operation={self.operation!r}, messages=[REDACTED], "
            f"max_output_units={self.max_output_units!r}, "
            f"temperature={self.temperature!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelOutputEvent:
    """One bounded text delta or completion marker from an adapter."""

    kind: ModelOutputEventKind
    text: str | None = None

    def __post_init__(self) -> None:
        """Require text only on a bounded delta event."""
        if not isinstance(self.kind, ModelOutputEventKind):
            message = "A model output event kind is invalid."
            raise TypeError(message)
        if self.kind is ModelOutputEventKind.COMPLETED:
            if self.text is not None:
                message = "A completion event must not contain model output."
                raise ValueError(message)
            return
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text.encode("utf-8")) > MAXIMUM_OUTPUT_DELTA_BYTES
        ):
            message = "A model output delta is invalid or too large."
            raise ValueError(message)

    def __repr__(self) -> str:
        """Do not put model output in a diagnostic representation."""
        return f"ModelOutputEvent(kind={self.kind!r}, text=[REDACTED])"


class ModelRequestSource(Protocol):
    """Load admitted provider-neutral input for one attempt."""

    def __call__(self, plan: AttemptPlan) -> ModelAdapterRequest:
        """Return the exact admitted request without provider settings."""
        ...


class CredentialLeaseSource(Protocol):
    """Lease the configured credential only for one provider attempt."""

    def __call__(self, plan: AttemptPlan) -> SecretLease:
        """Return one bounded secret lease for the plan credential generation."""
        ...


class ModelOutputSink(Protocol):
    """Accept provider-neutral text events for durable stream handling."""

    def __call__(self, plan: AttemptPlan, event: ModelOutputEvent) -> None:
        """Store or publish one bounded event under the request scope."""
        ...

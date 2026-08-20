"""Closed native model-request HTTP values."""
# ruff: noqa: EM101, PLR0913, PLR2004, TC001, TRY003

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from llmrouter_backend.adapters import ModelAdapterRequest
from llmrouter_backend.authority import RequestContext, Scope
from llmrouter_backend.execution import ExecutionState

MAXIMUM_HTTP_BODY_BYTES = 8 * 1024 * 1024
MAXIMUM_AUTHORIZATION_CHARACTERS = 207
MAXIMUM_REQUEST_ID_CHARACTERS = 36
MAXIMUM_LAST_EVENT_ID_CHARACTERS = 20
MAXIMUM_STATUS_BYTES = 8 * 1024 * 1024
MAXIMUM_STATUS_ATTEMPTS = 8

OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=200)]
AssignmentName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9.-]{0,99}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


class ClosedModel(BaseModel):
    """Reject unknown public fields and mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Money(ClosedModel):
    """One exact nonnegative cost control."""

    amount: Annotated[Decimal, Field(ge=0, max_digits=38, decimal_places=18)]
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount_string(cls, value: object) -> object:
        """Require the decimal string form in the public contract."""
        if (
            not isinstance(value, str)
            or re.fullmatch(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$", value) is None
        ):
            raise ValueError("A money amount must be a nonnegative decimal string.")
        return value


class RequestLimits(ClosedModel):
    """Fixed native model execution limits."""

    attempt_timeout_ms: Annotated[int, Field(ge=100, le=120_000, strict=True)]
    max_output_units: Annotated[int, Field(ge=1, le=1_000_000, strict=True)]
    max_cost: Money | None = None
    logical_timeout_ms: Literal[900_000] | None = None


class OutputControls(ClosedModel):
    """Provider-neutral text output controls."""

    format: Literal["text", "json"] | None = None
    json_schema_name: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[a-z][a-z0-9._-]{0,99}$"),
        ]
        | None
    ) = None
    json_schema_major_version: Annotated[int, Field(ge=1, strict=True)] | None = None
    temperature: Annotated[Decimal, Field(ge=0, le=2)] | None = None

    @field_validator("temperature", mode="before")
    @classmethod
    def validate_numeric_temperature(cls, value: object) -> object:
        """Reject a string where the contract requires a JSON number."""
        if value is not None and (
            isinstance(value, (bool, str))
            or not isinstance(value, (Decimal, int, float))
        ):
            raise ValueError("A temperature must be a JSON number.")
        return value

    @model_validator(mode="after")
    def validate_json_controls(self) -> OutputControls:
        """Require the JSON schema controls as one complete pair."""
        has_name = self.json_schema_name is not None
        has_version = self.json_schema_major_version is not None
        if has_name != has_version:
            raise ValueError("JSON schema controls must contain a name and version.")
        if (has_name or has_version) and self.format != "json":
            raise ValueError("JSON schema controls require JSON output.")
        return self


class TraceContext(ClosedModel):
    """Transient distributed trace values."""

    traceparent: Annotated[str, Field(max_length=55)] | None = None
    tracestate: Annotated[str, Field(max_length=512)] | None = None

    @model_validator(mode="after")
    def validate_traceparent(self) -> TraceContext:
        """Keep an invalid trace value out of downstream systems."""
        if (
            self.traceparent is not None
            and _TRACEPARENT.fullmatch(self.traceparent) is None
        ):
            raise ValueError("The traceparent value is invalid.")
        return self


class TextPart(ClosedModel):
    """One text message part."""

    type: Literal["text"]
    text: Annotated[str, Field(max_length=1_048_576, repr=False)]

    def __repr__(self) -> str:
        """Keep text content out of diagnostics."""
        return "TextPart(type='text', text=[REDACTED])"

    __str__ = __repr__


class ImagePart(ClosedModel):
    """One immutable image reference."""

    type: Literal["image"]
    attachment_id: OpaqueId
    sha256: Sha256
    media_type: Literal["image/jpeg", "image/png", "image/webp"]


class AudioPart(ClosedModel):
    """One immutable audio reference."""

    type: Literal["audio"]
    attachment_id: OpaqueId
    sha256: Sha256
    media_type: Literal["audio/mpeg", "audio/wav"]


class FilePart(ClosedModel):
    """One immutable document reference."""

    type: Literal["file"]
    attachment_id: OpaqueId
    sha256: Sha256
    media_type: Literal[
        "text/plain", "text/markdown", "application/json", "application/pdf"
    ]


ContentPart = Annotated[
    TextPart | ImagePart | AudioPart | FilePart,
    Field(discriminator="type"),
]


class Message(ClosedModel):
    """One bounded provider-neutral input message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: (
        Annotated[str, Field(max_length=1_048_576)]
        | Annotated[list[ContentPart], Field(max_length=100)]
    ) = Field(repr=False)

    def __repr__(self) -> str:
        """Keep message content out of diagnostics."""
        return f"Message(role={self.role!r}, content=[REDACTED])"

    __str__ = __repr__


class ToolDefinition(ClosedModel):
    """One registered tool declaration from the accepted contract."""

    name: Annotated[
        str,
        StringConstraints(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,99}$"),
    ]
    description: Annotated[str, Field(max_length=4_000, repr=False)]
    input_schema_name: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9._-]{0,99}$"),
    ]
    input_schema_major_version: Annotated[int, Field(ge=1, strict=True)]


class CompatibilityResponseFormat(ClosedModel):
    """One accepted compatibility output control."""

    type: Literal["text", "json_object", "json_schema"]
    schema_name: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9._-]{0,99}$")]
        | None
    ) = None
    schema_major_version: Annotated[int, Field(ge=1, strict=True)] | None = None


class MetadataEntry(ClosedModel):
    """One bounded compatibility metadata entry."""

    key: Annotated[str, Field(min_length=1, max_length=100)]
    value: Annotated[str, Field(max_length=1_000)]


class CompatibleChatRequest(ClosedModel):
    """The accepted OpenAI-compatible chat request."""

    model: AssignmentName
    messages: Annotated[
        list[Message], Field(min_length=1, max_length=1_000, repr=False)
    ]
    tools: Annotated[list[ToolDefinition], Field(max_length=100)] | None = Field(
        default=None, repr=False
    )
    tool_choice: Literal["auto", "none", "required"] | dict[str, object] | None = None
    response_format: CompatibilityResponseFormat | None = None
    temperature: Annotated[Decimal, Field(ge=0, le=2)] | None = None
    max_completion_tokens: Annotated[
        int, Field(ge=1, le=1_000_000, strict=True)
    ] | None = None
    stream: Annotated[bool, Field(strict=True)] = False
    metadata: Annotated[list[MetadataEntry], Field(max_length=100)] | None = None
    user: Annotated[str, Field(max_length=200)] | None = None
    x_llmrouter_workspace_id: OpaqueId | None = None
    x_llmrouter_data_profile: Literal["service-data"] | None = None
    x_llmrouter_max_cost: Money | None = None
    x_llmrouter_exact_route: OpaqueId | None = None
    x_llmrouter_exact_route_grant: (
        Annotated[SecretStr, Field(min_length=43, max_length=200)] | None
    ) = None

    @field_validator("temperature", mode="before")
    @classmethod
    def validate_numeric_temperature(cls, value: object) -> object:
        """Reject a string where the contract requires a JSON number."""
        if value is not None and (
            isinstance(value, (bool, str))
            or not isinstance(value, (Decimal, int, float))
        ):
            raise ValueError("A temperature must be a JSON number.")
        return value

    @model_validator(mode="after")
    def validate_exact_route_pair(self) -> CompatibleChatRequest:
        """Require both write-only exact-route extension fields together."""
        if (self.x_llmrouter_exact_route is None) != (
            self.x_llmrouter_exact_route_grant is None
        ):
            raise ValueError("An exact route requires one diagnostic grant.")
        return self

    def __repr__(self) -> str:
        """Keep compatibility content and grants out of diagnostics."""
        return (
            "CompatibleChatRequest("
            f"model={self.model!r}, messages=[REDACTED], tools=[REDACTED], "
            f"stream={self.stream!r}, workspace={self.x_llmrouter_workspace_id!r}, "
            "exact_route_grant=[REDACTED])"
        )

    __str__ = __repr__


class ModelRequestDocument(ClosedModel):
    """The accepted closed native request body."""

    api_version: Literal["1"]
    data_profile: Literal["service-data"]
    workspace_id: OpaqueId | None = None
    assignment: AssignmentName | None = None
    exact_route: OpaqueId | None = None
    exact_route_grant: (
        Annotated[SecretStr, Field(min_length=43, max_length=200)] | None
    ) = None
    messages: Annotated[
        list[Message], Field(min_length=1, max_length=1_000, repr=False)
    ]
    tools: Annotated[list[ToolDefinition], Field(max_length=100)] | None = Field(
        default=None, repr=False
    )
    tool_allow_list: list[OpaqueId] | None = None
    limits: RequestLimits
    output: OutputControls
    trace_context: TraceContext | None = None

    @model_validator(mode="after")
    def validate_target(self) -> ModelRequestDocument:
        """Require exactly one normal assignment or diagnostic target."""
        assignment = self.assignment is not None
        diagnostic = self.exact_route is not None or self.exact_route_grant is not None
        if assignment == diagnostic:
            raise ValueError("Select one assignment or one exact route and grant.")
        if diagnostic and (self.exact_route is None or self.exact_route_grant is None):
            raise ValueError("An exact route requires one diagnostic grant.")
        if self.tool_allow_list is not None and len(set(self.tool_allow_list)) != len(
            self.tool_allow_list
        ):
            raise ValueError("Tool allow-list values must be unique.")
        return self

    def __repr__(self) -> str:
        """Do not expose input content or a diagnostic grant."""
        return (
            "ModelRequestDocument("
            f"api_version={self.api_version!r}, data_profile={self.data_profile!r}, "
            f"workspace_id={self.workspace_id!r}, assignment={self.assignment!r}, "
            f"exact_route={self.exact_route!r}, exact_route_grant=[REDACTED], "
            "messages=[REDACTED], tools=[REDACTED], "
            f"tool_allow_list={self.tool_allow_list!r}, limits={self.limits!r}, "
            f"output={self.output!r}, trace_context={self.trace_context!r})"
        )

    __str__ = __repr__


class CancelDocument(ClosedModel):
    """One bounded cancellation reason."""

    reason: Annotated[str, Field(min_length=1, max_length=500, repr=False)]

    def __repr__(self) -> str:
        """Keep the cancellation reason out of diagnostics."""
        return "CancelDocument(reason=[REDACTED])"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class PreparedModelRequest:
    """Validated request values kept only while provider work can use them."""

    context: RequestContext
    scope: Scope
    adapter_request: ModelAdapterRequest = field(repr=False)

    def __repr__(self) -> str:
        """Keep all provider input out of diagnostics."""
        return (
            "PreparedModelRequest("
            f"context={self.context!r}, scope={self.scope!r}, "
            "adapter_request=[REDACTED])"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class CreateModelRequestResult:
    """One create or equal-replay HTTP result."""

    status_code: Literal[200, 201]
    receipt: dict[str, object]


@dataclass(frozen=True, slots=True)
class ScopedRequest:
    """An authenticated operation and its resolved request scope."""

    context: RequestContext
    scope: Scope


@dataclass(frozen=True, slots=True)
class ResumePoint:
    """One exact durable lifecycle point used for replay recovery."""

    state: ExecutionState
    state_revision: int


@dataclass(frozen=True, slots=True)
class FieldError:
    """One bounded safe public validation error."""

    path: str
    code: str
    message: str


class ModelRequestError(RuntimeError):
    """One safe native API failure."""

    __slots__ = (
        "code",
        "field_errors",
        "request_id",
        "retry_after_ms",
        "retryable",
        "status_code",
    )

    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        request_id: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        field_errors: tuple[FieldError, ...] = (),
    ) -> None:
        """Set only bounded content-free error values."""
        if not request_id or len(request_id) > 200:
            raise ValueError("A public error request identity is invalid.")
        if not code or len(code) > 100:
            raise ValueError("A public error code is invalid.")
        if not message or len(message) > 1_000:
            raise ValueError("A public error message is invalid.")
        if retry_after_ms is not None and not 0 <= retry_after_ms <= 900_000:
            raise ValueError("A public retry delay is invalid.")
        if len(field_errors) > 100:
            raise ValueError("A public error has too many field errors.")
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.field_errors = field_errors

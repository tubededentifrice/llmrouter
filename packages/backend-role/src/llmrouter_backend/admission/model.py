"""Closed values for request fingerprinting and durable admission."""
# ruff: noqa: C901, D105, EM101, PLR0912, PLR2004, TRY003

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import cast

import rfc8785

FINGERPRINT_NAME = "rfc8785-sha256-v1"
FINGERPRINT_VERSION = 1
DEFAULT_MAXIMUM_INITIAL_AGE = timedelta(minutes=15)
DEFAULT_MAXIMUM_FUTURE_SKEW = timedelta(minutes=5)
MINIMUM_TERMINAL_RETENTION = timedelta(hours=24)
_UUID_V7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ATTACHMENT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/json",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "audio/mpeg",
        "audio/wav",
    }
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Mapping[str, JsonValue] | Sequence[JsonValue]


class RequestKind(StrEnum):
    """Provider-neutral logical request kinds."""

    MODEL = "model"
    SHARED_TOOL = "shared_tool"


class RequestState(StrEnum):
    """States returned by admission and status operations."""

    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    """One validated immutable attachment fingerprint identity."""

    attachment_id: str
    sha256: str
    media_type: str
    byte_length: int

    def __post_init__(self) -> None:
        _require_text(self.attachment_id)
        if len(self.sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.sha256
        ):
            raise ValueError("An attachment digest must be lowercase SHA-256.")
        _require_text(self.media_type)
        if self.media_type not in _ATTACHMENT_MEDIA_TYPES:
            raise ValueError("An attachment media type is not supported.")
        if not 1 <= self.byte_length <= 25 * 1024 * 1024:
            raise ValueError("An attachment byte length is outside the fixed limit.")


@dataclass(frozen=True, slots=True)
class FingerprintInput:
    """The complete closed top-level request fingerprint document."""

    operation: str
    contract_major: int
    service_id: str
    workspace_id: str | None
    data_profile: str
    execution: Mapping[str, JsonValue]
    attachments: tuple[AttachmentReference, ...] = ()
    resolved_exact_route_scope: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        for value in (self.operation, self.service_id, self.data_profile):
            _require_text(value)
        if self.data_profile != "service-data":
            raise ValueError("The data profile is not supported.")
        if self.workspace_id is not None:
            _require_text(self.workspace_id)
        if self.contract_major < 1:
            raise ValueError("The contract major version must be positive.")
        if len(self.attachments) > 20:
            raise ValueError("A request contains too many attachments.")
        if sum(item.byte_length for item in self.attachments) > 100 * 1024 * 1024:
            raise ValueError("The request attachment bytes exceed the fixed limit.")
        if len({item.attachment_id for item in self.attachments}) != len(
            self.attachments
        ):
            raise ValueError("An attachment identity must be unique in one request.")
        object.__setattr__(self, "execution", _freeze_object(self.execution))
        _validate_execution_fields(self.operation, self.execution)
        _validate_attachment_references(
            self.operation, self.execution, self.attachments
        )
        if self.operation in {"model.create", "tool.create"} and (
            self.execution.get("api_version") != str(self.contract_major)
        ):
            raise ValueError("The request API version does not match its contract.")
        if (
            self.execution.get("x_llmrouter_workspace_id", self.workspace_id)
            != self.workspace_id
            or self.execution.get("x_llmrouter_data_profile", self.data_profile)
            != self.data_profile
        ):
            raise ValueError("A compatibility scope field does not match authority.")
        if self.resolved_exact_route_scope is not None:
            if frozenset(self.resolved_exact_route_scope) != {
                "service_id",
                "workspace_id",
                "exact_route_id",
            }:
                raise ValueError("The exact-route permission scope is incomplete.")
            object.__setattr__(
                self,
                "resolved_exact_route_scope",
                _freeze_object(self.resolved_exact_route_scope),
            )

    def document(self) -> dict[str, JsonValue]:
        """Return the exact JSON value that RFC 8785 canonicalizes."""
        return {
            "attachments": [
                {
                    "attachment_id": item.attachment_id,
                    "byte_length": item.byte_length,
                    "media_type": item.media_type,
                    "sha256": item.sha256,
                }
                for item in sorted(
                    self.attachments, key=lambda value: value.attachment_id
                )
            ],
            "authenticated_scope": {
                "service_id": self.service_id,
                "workspace_id": self.workspace_id,
            },
            "contract_major": self.contract_major,
            "data_profile": self.data_profile,
            "execution": _thaw(self.execution),
            "operation": self.operation,
            "resolved_exact_route_scope": _thaw(self.resolved_exact_route_scope),
        }

    def canonical_bytes(self) -> bytes:
        """Return RFC 8785 bytes without retaining them in a binding."""
        return rfc8785.dumps(self.document())

    def sha256(self) -> bytes:
        """Return the version-one collision-resistant request digest."""
        return hashlib.sha256(self.canonical_bytes()).digest()


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """One first-submit or replay admission request."""

    request_id: str
    kind: RequestKind
    fingerprint: FingerprintInput
    assignment: str | None = None
    exact_route_id: str | None = None
    capture_enabled: bool = True
    capture_reason: str = "configured"
    capture_policy: str | None = None
    captured_content_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_uuidv7(self.request_id)
        accepted_operations = {
            RequestKind.MODEL: {
                "model.create",
                "openai.chat.completions.create",
                "openai.responses.create",
            },
            RequestKind.SHARED_TOOL: {"tool.create"},
        }
        if self.fingerprint.operation not in accepted_operations[self.kind]:
            raise ValueError("The request kind and fingerprint operation do not match.")
        if (self.assignment is None) == (self.exact_route_id is None):
            raise ValueError("Select exactly one assignment or exact route.")
        if self.assignment is not None:
            _require_text(self.assignment)
        if self.exact_route_id is not None:
            _require_text(self.exact_route_id)
        if self.capture_policy is None:
            object.__setattr__(
                self,
                "capture_policy",
                "complete" if self.capture_enabled else "disabled",
            )
        if self.capture_policy not in {"complete", "metadata_only", "disabled"}:
            raise ValueError("The capture policy is not supported.")
        if self.capture_reason not in {"configured", "spool_pressure"}:
            raise ValueError("The capture reason is not supported.")
        if self.capture_enabled != (self.capture_policy != "disabled"):
            raise ValueError("The capture state and reason do not match.")
        if (
            self.capture_reason == "spool_pressure"
            and self.capture_policy != "disabled"
        ):
            raise ValueError("Spool pressure can only disable capture.")
        if self.captured_content_expires_at is not None and (
            self.captured_content_expires_at.tzinfo is None
            or self.captured_content_expires_at.utcoffset() is None
            or self.capture_policy == "disabled"
        ):
            raise ValueError("The captured-content expiry is invalid.")
        target_name = self.fingerprint.execution.get("assignment")
        if target_name is None:
            target_name = self.fingerprint.execution.get("model")
        if self.assignment is not None and target_name != self.assignment:
            raise ValueError("The selected assignment must match the fingerprint.")
        if self.exact_route_id is not None and (
            self.fingerprint.execution.get("exact_route") != self.exact_route_id
            and self.fingerprint.execution.get("x_llmrouter_exact_route")
            != self.exact_route_id
        ):
            raise ValueError("The selected exact route must match the fingerprint.")
        permission_scope = self.fingerprint.resolved_exact_route_scope
        if self.exact_route_id is None and permission_scope is not None:
            raise ValueError("An assignment must not contain exact-route permission.")
        if self.exact_route_id is not None and (
            permission_scope is None
            or permission_scope.get("service_id") != self.fingerprint.service_id
            or permission_scope.get("workspace_id") != self.fingerprint.workspace_id
            or permission_scope.get("exact_route_id") != self.exact_route_id
        ):
            raise ValueError("The exact-route permission scope does not match.")


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """The durable receipt returned before an external effect can start."""

    request_id: str
    admitted_at: datetime
    state: RequestState
    state_revision: int
    status_url: str
    cancel_url: str | None
    events_url: str | None
    fingerprint_version: str = FINGERPRINT_NAME
    capture_enabled: bool = True
    capture_reason: str = "configured"
    capture_policy: str = "complete"
    captured_content_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """A durable admission and its create-or-replay outcome."""

    receipt: AdmissionReceipt
    created: bool
    durable: bool = field(default=True, init=False)

    @property
    def external_effects_permitted(self) -> bool:
        """Confirm that this result came from the durable admission boundary."""
        return self.durable and self.created


@dataclass(frozen=True, slots=True)
class RequestStatus:
    """The bounded admission status available in the original scope."""

    receipt: AdmissionReceipt
    last_transition_at: datetime
    terminal_at: datetime | None
    configuration_revision_id: str
    assignment_id: str | None
    exact_route_id: str | None


def validate_uuidv7(value: str) -> uuid.UUID:
    """Require a canonical UUIDv7 with the RFC variant."""
    if _UUID_V7.fullmatch(value) is None:
        raise ValueError("The request identity must be a canonical UUIDv7.")
    parsed = uuid.UUID(value)
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ValueError("The request identity must use UUIDv7 and the RFC variant.")
    random_a = (parsed.int >> 64) & ((1 << 12) - 1)
    random_b = parsed.int & ((1 << 62) - 1)
    if random_a == 0 and random_b == 0:
        raise ValueError("The request UUIDv7 must contain opaque random bits.")
    return parsed


def uuidv7_time(value: str) -> datetime:
    """Return the UTC time from the UUIDv7 48-bit Unix millisecond field."""
    parsed = validate_uuidv7(value)
    milliseconds = parsed.int >> 80
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _freeze_object(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("A fingerprint document must be a JSON object.")
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Each fingerprint object key must be text.")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("A fingerprint number must be finite.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("A fingerprint contains a value that JSON does not support.")


def _thaw(value: JsonValue | None) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw(item) for item in value]
    return value


def _require_text(value: str) -> None:
    if not value or len(value) > 500:
        raise ValueError("A fingerprint text identity is empty or too large.")


_EXECUTION_SCHEMAS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "model.create": (
        frozenset({"api_version", "messages", "limits", "output"}),
        frozenset(
            {
                "api_version",
                "assignment",
                "exact_route",
                "messages",
                "tools",
                "tool_allow_list",
                "limits",
                "output",
            }
        ),
    ),
    "tool.create": (
        frozenset({"api_version", "assignment", "tool", "input", "limits"}),
        frozenset({"api_version", "assignment", "tool", "input", "limits"}),
    ),
    "openai.chat.completions.create": (
        frozenset({"model", "messages"}),
        frozenset(
            {
                "model",
                "messages",
                "tools",
                "tool_choice",
                "response_format",
                "temperature",
                "max_completion_tokens",
                "metadata",
                "user",
                "x_llmrouter_max_cost",
                "x_llmrouter_exact_route",
                "x_llmrouter_workspace_id",
                "x_llmrouter_data_profile",
            }
        ),
    ),
    "openai.responses.create": (
        frozenset({"model", "input"}),
        frozenset(
            {
                "model",
                "input",
                "instructions",
                "tools",
                "tool_choice",
                "text",
                "temperature",
                "max_output_tokens",
                "metadata",
                "user",
                "x_llmrouter_max_cost",
                "x_llmrouter_exact_route",
                "x_llmrouter_workspace_id",
                "x_llmrouter_data_profile",
            }
        ),
    ),
}


def _validate_execution_fields(
    operation: str, execution: Mapping[str, JsonValue]
) -> None:
    schema = _EXECUTION_SCHEMAS.get(operation)
    if schema is None:
        raise ValueError("The fingerprint operation is not supported.")
    required, allowed = schema
    names = frozenset(execution)
    if not required <= names or not names <= allowed:
        raise ValueError("The fingerprint execution fields are incomplete or unknown.")
    if operation == "model.create" and (
        ("assignment" in names) == ("exact_route" in names)
    ):
        raise ValueError("The model fingerprint must select one request target.")


def _validate_attachment_references(
    operation: str,
    execution: Mapping[str, JsonValue],
    attachments: tuple[AttachmentReference, ...],
) -> None:
    """Require the validated attachment set to match all message references."""
    message_values: JsonValue | None = execution.get("messages")
    if operation == "openai.responses.create":
        response_input = execution.get("input")
        message_values = None if isinstance(response_input, str) else response_input
    referenced: set[tuple[str, str, str]] = set()
    if isinstance(message_values, Sequence) and not isinstance(
        message_values, (str, bytes, bytearray)
    ):
        for message in message_values:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if not isinstance(content, Sequence) or isinstance(
                content, (str, bytes, bytearray)
            ):
                continue
            for part in content:
                if not isinstance(part, Mapping) or part.get("type") not in {
                    "image",
                    "audio",
                    "file",
                }:
                    continue
                identity = part.get("attachment_id")
                digest = part.get("sha256")
                media_type = part.get("media_type")
                if not all(
                    isinstance(item, str) for item in (identity, digest, media_type)
                ):
                    raise ValueError("An attachment content part is incomplete.")
                referenced.add(
                    (
                        cast("str", identity),
                        cast("str", digest),
                        cast("str", media_type),
                    )
                )
    declared = {
        (item.attachment_id, item.sha256, item.media_type) for item in attachments
    }
    if referenced != declared:
        raise ValueError(
            "The validated attachments do not match the message references."
        )

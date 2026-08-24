"""Closed native model-call HTTP composition."""
# ruff: noqa: D102, EM101, PLR2004, TC003, TRY003, TRY004

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math

# A fixed isolated child bounds schema evaluation.
import subprocess  # nosec B404
import sys
import threading
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from opendle import AssignmentSelector, ExactModelSelector, RouterContractError
from pydantic import ConfigDict, Field, field_validator, model_validator
from referencing import Registry

from llmrouter_backend.calls import (
    CallRequest,
    CallRequirements,
    CallResult,
    OutputValidationUnavailableError,
    OutputValidator,
)
from llmrouter_backend.diagnostics import CapturedMedia
from llmrouter_backend.models import ClosedModel, Usage, UsageItem, UsageUnit

_API_NAME_PATTERN = r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_ASSIGNMENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,126}$"
_MAXIMUM_JSON_TEXT = 1_000_000
_MAXIMUM_MODEL_JSON_BYTES = 2 * 1024 * 1024
_MAXIMUM_IMAGE_BYTES = 20 * 1024 * 1024
_MAXIMUM_IMAGE_SET_BYTES = 50 * 1024 * 1024
_MAXIMUM_IMAGES = 8
_SCHEMA_REGISTRY: Registry[bool | Mapping[str, Any]] = Registry()
_STRUCTURED_VALIDATION_TIMEOUT_SECONDS = 2
_STRUCTURED_VALIDATION_SLOTS = threading.BoundedSemaphore(4)
_STRUCTURED_VALIDATION_PROGRAM = """
import json
import resource
import sys
from jsonschema import Draft202012Validator
from referencing import Registry

resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
try:
    payload = json.load(sys.stdin)
    valid = Draft202012Validator(
        payload["schema"], registry=Registry()
    ).is_valid(payload["value"])
except BaseException:
    raise SystemExit(2) from None
raise SystemExit(0 if valid else 1)
"""


class NativeModel(ClosedModel):
    """Reject coercion and hide private input values in validation diagnostics."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class AssignmentModelSelector(NativeModel):
    """Select one named assignment."""

    assignment_api_name: str = Field(pattern=_ASSIGNMENT_NAME_PATTERN)


class ExactProviderModelSelector(NativeModel):
    """Select one exact enabled provider-model."""

    provider_model_api_name: str = Field(pattern=_API_NAME_PATTERN)


type ModelSelector = AssignmentModelSelector | ExactProviderModelSelector


class TextInputPart(NativeModel):
    """One non-empty user text part."""

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=_MAXIMUM_JSON_TEXT)

    @field_validator("text")
    @classmethod
    def require_utf8(cls, value: str) -> str:
        _utf8(value)
        return value


class ImageInputPart(NativeModel):
    """One bounded uploaded JPEG, PNG, or WebP image."""

    type: Literal["image"]
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    data_base64: str = Field(min_length=1, max_length=27_962_028)

    @model_validator(mode="after")
    def validate_image(self) -> ImageInputPart:
        body = self.decoded_body()
        if not 1 <= len(body) <= _MAXIMUM_IMAGE_BYTES:
            raise ValueError("The image byte size is invalid.")
        if not _matches_media_type(body, self.media_type):
            raise ValueError("The image bytes do not match the media type.")
        return self

    def decoded_body(self) -> bytes:
        """Decode only canonical standard Base64 input."""
        try:
            body = base64.b64decode(self.data_base64, validate=True)
        except binascii.Error, ValueError:
            raise ValueError("The image data is not valid Base64.") from None
        if base64.b64encode(body).decode("ascii") != self.data_base64:
            raise ValueError("The image data is not canonical Base64.")
        return body


class ToolResultPart(NativeModel):
    """One caller-owned tool result."""

    type: Literal["tool_result"]
    tool_call_id: str = Field(min_length=1, max_length=200)
    result_json: str = Field(min_length=1, max_length=_MAXIMUM_JSON_TEXT)

    @field_validator("result_json")
    @classmethod
    def require_json(cls, value: str) -> str:
        _load_finite_json(value)
        return value


type UserContentPart = Annotated[
    TextInputPart | ImageInputPart | ToolResultPart,
    Field(discriminator="type"),
]


class TextOutputPart(NativeModel):
    """One prior assistant text part."""

    type: Literal["text"]
    text: str

    @field_validator("text")
    @classmethod
    def require_utf8(cls, value: str) -> str:
        _utf8(value)
        return value


class ToolCallPart(NativeModel):
    """One prior assistant or returned tool call."""

    type: Literal["tool_call"]
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    arguments_json: str = Field(min_length=1, max_length=_MAXIMUM_JSON_TEXT)

    @field_validator("arguments_json")
    @classmethod
    def require_json_object(cls, value: str) -> str:
        if not isinstance(_load_finite_json(value), dict):
            raise ValueError("Tool arguments must be one JSON object.")
        return value


type AssistantContentPart = Annotated[
    TextOutputPart | ToolCallPart,
    Field(discriminator="type"),
]


class SystemMessage(NativeModel):
    """One system message."""

    role: Literal["system"]
    content: str = Field(min_length=1, max_length=_MAXIMUM_JSON_TEXT)

    @field_validator("content")
    @classmethod
    def require_utf8(cls, value: str) -> str:
        _utf8(value)
        return value


class UserMessage(NativeModel):
    """One user message."""

    role: Literal["user"]
    content: list[UserContentPart] = Field(min_length=1)


class AssistantMessage(NativeModel):
    """One prior assistant message."""

    role: Literal["assistant"]
    content: list[AssistantContentPart] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_tool_call_ids(self) -> AssistantMessage:
        values = [part.id for part in self.content if isinstance(part, ToolCallPart)]
        if len(values) != len(set(values)):
            raise ValueError("Tool call identifiers must be unique in one message.")
        return self


type ModelMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage,
    Field(discriminator="role"),
]


class ToolDefinition(NativeModel):
    """One caller-owned tool definition."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    input_schema_json: str = Field(min_length=2, max_length=100_000)

    @field_validator("input_schema_json")
    @classmethod
    def require_json_schema(cls, value: str) -> str:
        _compile_schema(value)
        return value


class OutputFormat(NativeModel):
    """Select normal text or one JSON Schema result."""

    type: Literal["text", "json_schema"]
    schema_document: str | None = Field(
        default=None, alias="schema_json", min_length=2, max_length=100_000
    )

    @model_validator(mode="after")
    def validate_shape(self) -> OutputFormat:
        if self.type == "text" and "schema_document" in self.model_fields_set:
            raise ValueError("Text output cannot contain a JSON Schema.")
        if self.type == "json_schema" and self.schema_document is None:
            raise ValueError("Structured output requires one JSON Schema.")
        if self.schema_document is not None:
            _compile_schema(self.schema_document)
        return self


class ModelCallRequest(NativeModel):
    """One complete closed native model-call body."""

    workspace_api_name: str = Field(pattern=_API_NAME_PATTERN)
    selector: ModelSelector
    excluded_provider_model_api_names: list[str] | None = Field(
        default=None, max_length=16
    )
    messages: list[ModelMessage] = Field(min_length=1, max_length=1000)
    tools: list[ToolDefinition] | None = Field(default=None, max_length=128)
    output_format: OutputFormat | None = None
    output_limit: int | None = Field(default=None, strict=True, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, strict=True, ge=0, le=2)
    tags: list[str] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_complete_call(self) -> ModelCallRequest:
        optional_fields = {
            "excluded_provider_model_api_names",
            "tools",
            "output_format",
            "output_limit",
            "temperature",
            "tags",
        }
        if any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in optional_fields
        ):
            raise ValueError("An optional model-call field cannot be null.")
        excluded = self.excluded_provider_model_api_names or []
        if any(not _api_name(value) for value in excluded):
            raise ValueError("An excluded provider-model API name is invalid.")
        if len(excluded) != len(set(excluded)):
            raise ValueError("Excluded provider-model names must be unique.")
        if excluded and not isinstance(self.selector, AssignmentModelSelector):
            raise ValueError("Only an assignment call can exclude provider-models.")
        tools = self.tools or []
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Tool names must be unique.")
        images = self.images()
        if len(images) > _MAXIMUM_IMAGES:
            raise ValueError("A model call can contain no more than 8 images.")
        image_bytes = sum(len(image.decoded_body()) for image in images)
        if image_bytes > _MAXIMUM_IMAGE_SET_BYTES:
            raise ValueError("The model call image byte total is too large.")
        if self.temperature is not None and not math.isfinite(self.temperature):
            raise ValueError("The temperature must be finite.")
        _validate_tags(self.tags or [])
        model_json_bytes = len(self.sanitized_request_json().encode("utf-8"))
        if model_json_bytes > _MAXIMUM_MODEL_JSON_BYTES:
            raise ValueError("The model call JSON body is too large.")
        return self

    def images(self) -> tuple[ImageInputPart, ...]:
        """Return uploaded images in message and part order."""
        return tuple(
            part
            for message in self.messages
            if isinstance(message, UserMessage)
            for part in message.content
            if isinstance(part, ImageInputPart)
        )

    def sanitized_request_json(self) -> str:
        """Keep input media bytes outside the structured request log."""
        value = self.model_dump(mode="json", exclude_none=True, by_alias=True)
        messages = cast("list[dict[str, object]]", value["messages"])
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image":
                    part["data_base64"] = ""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class StandardModelCallResult(NativeModel):
    """One normal text or tool-call result."""

    output_type: Literal["standard"]
    provider_model_api_name: str = Field(pattern=_API_NAME_PATTERN)
    content: list[AssistantContentPart] = Field(min_length=1)
    usage: Usage


class StructuredModelCallResult(NativeModel):
    """One schema-validated JSON result."""

    output_type: Literal["structured_json"]
    provider_model_api_name: str = Field(pattern=_API_NAME_PATTERN)
    structured_output_json: str = Field(min_length=1, max_length=_MAXIMUM_JSON_TEXT)
    usage: Usage


type ModelCallResult = StandardModelCallResult | StructuredModelCallResult


def internal_model_call(body: ModelCallRequest, *, streaming: bool) -> CallRequest:
    """Translate one validated HTTP body without provider-specific fields."""
    if (
        streaming
        and body.output_format is not None
        and body.output_format.type == "json_schema"
    ):
        raise ValueError("A model stream cannot request structured JSON output.")
    selector = (
        AssignmentSelector(body.selector.assignment_api_name)
        if isinstance(body.selector, AssignmentModelSelector)
        else ExactModelSelector(body.selector.provider_model_api_name)
    )
    images = body.images()
    output = (
        "structured_json"
        if body.output_format is not None and body.output_format.type == "json_schema"
        else "text"
    )
    required_capabilities: set[str] = set()
    if streaming:
        required_capabilities.add("streaming")
    if body.tools or any(
        isinstance(part, ToolCallPart | ToolResultPart)
        for message in body.messages
        if isinstance(message, UserMessage | AssistantMessage)
        for part in message.content
    ):
        required_capabilities.add("tool_calling")
    schema_validator = _output_validator(body.output_format)
    return CallRequest(
        workspace_api_name=body.workspace_api_name,
        selector=selector,
        kind="model",
        requirements=CallRequirements(
            frozenset({"text", "image"} if images else {"text"}),
            output,
            frozenset(required_capabilities),
            input_image_sizes=tuple(len(image.decoded_body()) for image in images),
        ),
        request_json=body.sanitized_request_json(),
        tags=tuple(body.tags or ()),
        excluded_provider_model_api_names=tuple(
            body.excluded_provider_model_api_names or ()
        ),
        streaming=streaming,
        output_validator=schema_validator,
        media=tuple(
            CapturedMedia(image.decoded_body(), image.media_type, "input")
            for image in images
        ),
    )


def model_call_result(result: CallResult) -> ModelCallResult:
    """Compose one exact closed synchronous response."""
    usage = _usage(result)
    if len(result.outputs) != 1:
        raise RuntimeError("A synchronous model call has no single result.")
    output = result.outputs[0]
    if output.kind == "structured_json":
        return StructuredModelCallResult(
            output_type="structured_json",
            provider_model_api_name=result.provider_model_api_name,
            structured_output_json=output.content_json,
            usage=usage,
        )
    if output.kind != "standard":
        raise RuntimeError("A synchronous model call has an invalid result kind.")
    content = cast("list[object]", _load_finite_json(output.content_json))
    return StandardModelCallResult.model_validate(
        {
            "output_type": "standard",
            "provider_model_api_name": result.provider_model_api_name,
            "content": content,
            "usage": usage.model_dump(mode="json"),
        }
    )


def result_usage(result: CallResult) -> dict[str, object]:
    """Return the closed stream-completion usage value."""
    return _usage(result).model_dump(mode="json")


def _usage(result: CallResult) -> Usage:
    return Usage(
        units=[
            UsageItem(
                unit=cast("UsageUnit", item.unit),
                quantity=_decimal_text(item.quantity),
            )
            for item in result.usage
        ],
        cost=_decimal_text(result.cost),
        currency=result.applied_price.currency,
    )


def _output_validator(output_format: OutputFormat | None) -> OutputValidator | None:
    if output_format is None or output_format.type == "text":
        return None
    schema = cast(
        "dict[str, object]", _load_finite_json(output_format.schema_document or "")
    )

    async def validate(value: object) -> bool:
        return await asyncio.to_thread(_validate_structured_output, schema, value)

    return validate


def _validate_structured_output(schema: dict[str, object], value: object) -> bool:
    """Validate in one bounded process so a caller schema cannot block the web loop."""
    payload = json.dumps(
        {"schema": schema, "value": value},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if not _STRUCTURED_VALIDATION_SLOTS.acquire(
        timeout=_STRUCTURED_VALIDATION_TIMEOUT_SECONDS
    ):
        raise OutputValidationUnavailableError(
            "The structured-output validator is at its safety limit."
        )
    try:
        try:
            # The executable and program are fixed. Only stdin is caller data.
            completed = subprocess.run(  # noqa: S603  # nosec B603
                (sys.executable, "-I", "-c", _STRUCTURED_VALIDATION_PROGRAM),
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_STRUCTURED_VALIDATION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OutputValidationUnavailableError(
                "The structured-output validator is unavailable."
            ) from error
    finally:
        _STRUCTURED_VALIDATION_SLOTS.release()
    if completed.returncode not in {0, 1}:
        raise OutputValidationUnavailableError(
            "The structured-output validator did not complete safely."
        )
    return completed.returncode == 0


def _compile_schema(value: str) -> dict[str, object]:
    document = _load_finite_json(value)
    if not isinstance(document, dict):
        raise ValueError("A JSON Schema must be one object.")
    try:
        Draft202012Validator.check_schema(document)
        # Construct with an empty registry so remote references cannot use the network.
        Draft202012Validator(document, registry=_SCHEMA_REGISTRY)
    except SchemaError as error:
        raise ValueError("The JSON Schema is invalid.") from error
    return cast("dict[str, object]", document)


def _load_finite_json(value: str) -> object:
    def reject_constant(_value: str) -> object:
        raise ValueError("A JSON number must be finite.")

    try:
        document = json.loads(value, parse_constant=reject_constant)
    except TypeError, ValueError, RecursionError:
        raise ValueError("The value is not valid JSON.") from None
    if not _finite_json(document):
        raise ValueError("A JSON number must be finite.")
    return document


def _finite_json(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_json(item) for key, item in value.items()
        )
    return value is None or isinstance(value, str | int | bool)


def _validate_tags(values: list[str]) -> None:
    try:
        normalized = sorted({_utf8(value): value for value in values}.items())
    except TypeError:
        raise ValueError("Each tag must be text.") from None
    if any(not 1 <= len(encoded) <= 128 for encoded, _value in normalized):
        raise ValueError("A tag byte size is invalid.")
    if sum(len(encoded) for encoded, _value in normalized) > 2048:
        raise ValueError("The normalized tag set is too large.")


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("The text is not valid UTF-8.") from None


def _api_name(value: object) -> bool:
    try:
        ExactModelSelector(cast("str", value))
    except RouterContractError, TypeError:
        return False
    return True


def _matches_media_type(body: bytes, media_type: str) -> bool:
    return (
        (media_type == "image/jpeg" and body.startswith(b"\xff\xd8\xff"))
        or (media_type == "image/png" and body.startswith(b"\x89PNG\r\n\x1a\n"))
        or (
            media_type == "image/webp"
            and len(body) >= 12
            and body.startswith(b"RIFF")
            and body[8:12] == b"WEBP"
        )
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"

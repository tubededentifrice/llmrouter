"""Deterministic, network-free provider adapter for conformance tests."""

from __future__ import annotations

import asyncio
import json
import math
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from opendle import CallFailurePhase

from llmrouter_backend.accounting import UsageAmount
from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureClass,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

_MAXIMUM_REQUEST_BYTES = 2 * 1024 * 1024
_MAXIMUM_TOOL_NAME = 200
_AUTHENTICATION: ProviderFailureClass = "authentication"
_INCOMPATIBLE: ProviderFailureClass = "incompatible"
_INTERRUPTED: ProviderFailureClass = "interrupted"
_TRANSPORT: ProviderFailureClass = "transport"
_MODEL_FIELDS = frozenset(
    {
        "workspace_api_name",
        "selector",
        "excluded_provider_model_api_names",
        "messages",
        "tools",
        "output_format",
        "output_limit",
        "temperature",
        "tags",
    }
)
_EMBEDDING_FIELDS = frozenset({"workspace_api_name", "selector", "inputs", "tags"})
_MEDIA_FIELDS = frozenset(
    {
        "workspace_api_name",
        "selector",
        "kind",
        "prompt",
        "input_images",
        "tags",
    }
)
_FAILURE_MODELS: Mapping[str, ProviderFailureClass] = {
    "fake-error-authentication-v1": "authentication",
    "fake-error-rate-v1": "rate_limited",
    "fake-error-timeout-v1": "timeout",
    "fake-error-transport-v1": "transport",
    "fake-error-unavailable-v1": "unavailable",
    "fake-error-refusal-v1": "refusal",
    "fake-error-incompatible-v1": "incompatible",
    "fake-error-invalid-response-v1": "invalid_response",
    "fake-error-interrupted-v1": "interrupted",
    "fake-error-upstream-v1": "upstream_failed",
}
_USAGE_UNITS = frozenset(
    {
        "input_token",
        "output_token",
        "cached_input_token",
        "image",
        "video_second",
        "audio_second",
        "request",
        "provider_unit",
    }
)


class FakeAdapter:
    """Return fixed native events without a secret, clock, random value, or network."""

    usage_units = _USAGE_UNITS

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Return only units that the selected fake operation can report."""
        return frozenset(item.unit for item in _usage(operation))

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Yield one deterministic attempt for the configured fake wire model."""
        if request.route.adapter != "fake":
            raise _failure(_INCOMPATIBLE)
        if request.credential is not None:
            raise _failure(_AUTHENTICATION)

        model = request.route.provider_model_name
        body = _request_body(request)
        if failure_class := _FAILURE_MODELS.get(model):
            raise _failure(failure_class)
        async for event in _model_events(model, request, body):
            yield event


async def _model_events(
    model: str,
    request: ProviderAttemptRequest,
    body: Mapping[str, object],
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if model in {"fake-stream-interruption-v1", "fake-media-uncertain-v1"}:
        async for event in _boundary_failure_events(model, request):
            yield event
        return
    if model == "fake-cancel-v1":
        await asyncio.Event().wait()
        return
    if model == "fake-duplicate-tool-ids-v1":
        async for event in _duplicate_tool_events(request):
            yield event
        return
    if model == "fake-cumulative-output-v1":
        async for event in _cumulative_output_events(request):
            yield event
        return
    async for event in _normal_events(model, request, body):
        yield event


async def _boundary_failure_events(
    model: str, request: ProviderAttemptRequest
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if model == "fake-stream-interruption-v1":
        if not request.streaming or request.kind != "model":
            raise _failure(_INCOMPATIBLE)
        yield ProviderOutput("text_delta", '"Fake visible output."')
        raise _failure(_INTERRUPTED)
    if request.kind != "media":
        raise _failure(_INCOMPATIBLE)
    raise _failure(_TRANSPORT, phase=CallFailurePhase.UNCERTAIN)


async def _normal_events(
    model: str,
    request: ProviderAttemptRequest,
    body: Mapping[str, object],
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if model == "fake-text-v1":
        async for event in _text_events(request, body):
            yield event
        return
    if model == "fake-embedding-v1":
        yield _embedding_output(request)
        yield ProviderCompleted(_usage(request.operation))
        return
    if model == "fake-media-v1":
        yield _media_output(request)
        yield ProviderCompleted(_usage(request.operation))
        return
    raise _failure(_INCOMPATIBLE)


async def _cumulative_output_events(
    request: ProviderAttemptRequest,
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if not request.streaming or request.kind != "model":
        raise _failure(_INCOMPATIBLE)
    for value in ("one", "two", "three"):
        yield ProviderOutput("text_delta", json.dumps(value * 32))
    yield ProviderCompleted(_usage(request.operation))


def _request_body(request: ProviderAttemptRequest) -> dict[str, object]:
    if tuple(len(item.body) for item in request.input_media) != (
        request.requirements.input_image_sizes
    ) or any(
        type(item.body) is not bytes
        or item.role != "input"
        or item.media_type not in {"image/jpeg", "image/png", "image/webp"}
        for item in request.input_media
    ):
        raise _failure(_INCOMPATIBLE)
    try:
        _validate_request_size(request.request_json)
        value = json.loads(
            request.request_json,
            parse_constant=_reject_constant,
        )
    except TypeError, UnicodeError, ValueError, RecursionError:
        raise _failure(_INCOMPATIBLE) from None
    if not isinstance(value, dict) or not _finite_json(value):
        raise _failure(_INCOMPATIBLE)
    body = cast("dict[str, object]", value)
    allowed = {
        "model": _MODEL_FIELDS,
        "embedding": _EMBEDDING_FIELDS,
        "media": _MEDIA_FIELDS,
    }[request.kind]
    if not set(body) <= allowed:
        raise _failure(_INCOMPATIBLE)
    if request.kind == "model" and not isinstance(body.get("messages"), list):
        raise _failure(_INCOMPATIBLE)
    if request.kind == "embedding":
        inputs = body.get("inputs")
        if (
            not isinstance(inputs, list)
            or len(inputs) != request.expected_embedding_count
            or any(not isinstance(item, str) or not item for item in inputs)
        ):
            raise _failure(_INCOMPATIBLE)
    if (
        request.kind == "media"
        and body.get("kind") != request.requirements.required_output
    ):
        raise _failure(_INCOMPATIBLE)
    return body


def _validate_request_size(value: str) -> None:
    if not 1 <= len(value.encode("utf-8")) <= _MAXIMUM_REQUEST_BYTES:
        raise ValueError


def _reject_constant(_value: str) -> None:
    raise ValueError


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


async def _text_events(
    request: ProviderAttemptRequest, body: Mapping[str, object]
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if request.kind != "model" or request.requirements.required_output not in {
        "text",
        "structured_json",
    }:
        raise _failure(_INCOMPATIBLE)
    if request.requirements.required_output == "structured_json":
        if request.streaming:
            raise _failure(_INCOMPATIBLE)
        yield ProviderOutput("structured_json", '{"result":"fake"}')
        yield ProviderCompleted(_usage(request.operation))
        return

    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise _failure(_INCOMPATIBLE)
    tool_names = _tool_names(tools)
    if request.streaming:
        if tool_names and "tool_calling" in request.requirements.required_capabilities:
            for index, name in enumerate(tool_names, start=1):
                yield ProviderOutput(
                    "tool_call",
                    json.dumps(
                        {
                            "type": "tool_call",
                            "id": f"fake-call-{index:03d}",
                            "name": name,
                            "arguments_json": "{}",
                        },
                        separators=(",", ":"),
                    ),
                )
        else:
            yield ProviderOutput("text_delta", '"Fake "')
            yield ProviderOutput("text_delta", '"response."')
    else:
        parts: list[dict[str, object]]
        if tool_names and "tool_calling" in request.requirements.required_capabilities:
            parts = [
                {
                    "type": "tool_call",
                    "id": f"fake-call-{index:03d}",
                    "name": name,
                    "arguments_json": "{}",
                }
                for index, name in enumerate(tool_names, start=1)
            ]
        else:
            parts = [{"type": "text", "text": "Fake response."}]
        yield ProviderOutput("standard", json.dumps(parts, separators=(",", ":")))
    yield ProviderCompleted(_usage(request.operation))


def _tool_names(tools: Sequence[object]) -> tuple[str, ...]:
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise _failure(_INCOMPATIBLE)
        name = cast("str", tool["name"])
        if not 1 <= len(name) <= _MAXIMUM_TOOL_NAME or name in names:
            raise _failure(_INCOMPATIBLE)
        names.append(name)
    return tuple(names)


def _embedding_output(request: ProviderAttemptRequest) -> ProviderOutput:
    dimension = request.requirements.embedding_dimension
    count = request.expected_embedding_count
    if request.kind != "embedding" or dimension is None or count is None:
        raise _failure(_INCOMPATIBLE)
    vectors = [
        [index if position == 0 else 0 for position in range(dimension)]
        for index in range(count)
    ]
    return ProviderOutput("embedding", json.dumps(vectors, separators=(",", ":")))


def _media_output(request: ProviderAttemptRequest) -> ProviderOutput:
    output = request.requirements.required_output
    if request.kind != "media" or output not in {"image", "video", "audio"}:
        raise _failure(_INCOMPATIBLE)
    media_type = {
        "image": "image/png",
        "video": "video/mp4",
        "audio": "audio/mpeg",
    }[output]
    return ProviderOutput(
        "media",
        json.dumps({"media_type": media_type, "size_bytes": 16}, separators=(",", ":")),
        b"fake-media-bytes",
    )


async def _duplicate_tool_events(
    request: ProviderAttemptRequest,
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    if (
        request.kind != "model"
        or "tool_calling" not in request.requirements.required_capabilities
    ):
        raise _failure(_INCOMPATIBLE)
    tool = {
        "type": "tool_call",
        "id": "duplicate",
        "name": "lookup",
        "arguments_json": "{}",
    }
    if request.streaming:
        yield ProviderOutput("tool_call", json.dumps(tool, separators=(",", ":")))
        yield ProviderOutput("tool_call", json.dumps(tool, separators=(",", ":")))
    else:
        yield ProviderOutput(
            "standard", json.dumps([tool, tool], separators=(",", ":"))
        )
    yield ProviderCompleted(_usage(request.operation))


def _usage(operation: ProviderOperation) -> tuple[UsageAmount, ...]:
    common = (
        UsageAmount("request", Decimal(1)),
        UsageAmount("provider_unit", Decimal("0.5")),
    )
    output = operation.requirements.required_output
    expected_kind = {
        "text": "model",
        "structured_json": "model",
        "embedding": "embedding",
        "image": "media",
        "video": "media",
        "audio": "media",
    }.get(output)
    if operation.kind != expected_kind:
        raise _failure(_INCOMPATIBLE)
    if output in {"text", "structured_json"}:
        return (
            UsageAmount("input_token", Decimal(4)),
            UsageAmount("cached_input_token", Decimal(1)),
            UsageAmount("output_token", Decimal(2)),
            *common,
        )
    if output == "embedding":
        return (UsageAmount("input_token", Decimal(3)), *common)
    if output == "image":
        return (UsageAmount("image", Decimal(1)), *common)
    if output == "video":
        return (UsageAmount("video_second", Decimal(5)), *common)
    if output == "audio":
        return (UsageAmount("audio_second", Decimal(3)), *common)
    raise _failure(_INCOMPATIBLE)


def _failure(
    failure_class: ProviderFailureClass,
    *,
    phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
) -> ProviderFailureError:
    return ProviderFailureError(failure_class, phase=phase)

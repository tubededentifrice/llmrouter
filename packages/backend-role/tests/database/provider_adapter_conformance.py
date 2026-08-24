"""Common assertions for each applicable native provider-adapter operation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from llmrouter_backend.calls import (
    ProviderAdapter,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureError,
    ProviderOutput,
)
from opendle import CallFailurePhase

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


@dataclass(frozen=True, slots=True)
class SuccessCase:
    """Name one provider-neutral operation and its required result facts."""

    name: str
    expected_output_kinds: tuple[str, ...]
    expected_usage_units: frozenset[str]


@dataclass(frozen=True, slots=True)
class AttemptCapture:
    """Keep events and one normalized failure from an adapter attempt."""

    events: tuple[ProviderOutput | ProviderCompleted, ...]
    failure: ProviderFailureError | None


@dataclass(frozen=True, slots=True)
class FailureCase:
    """Name one normalized failure and whether provider output preceded it."""

    failure_class: str
    visible_before_failure: bool
    phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT


SUCCESS_CASES = (
    SuccessCase(
        "standard_text",
        ("standard",),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "stream_text",
        ("text_delta", "text_delta"),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "buffered_tools",
        ("standard",),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "stream_tools",
        ("tool_call", "tool_call"),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "structured_json",
        ("structured_json",),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "input_image",
        ("standard",),
        frozenset(
            {
                "input_token",
                "cached_input_token",
                "output_token",
                "request",
                "provider_unit",
            }
        ),
    ),
    SuccessCase(
        "embedding",
        ("embedding",),
        frozenset({"input_token", "request", "provider_unit"}),
    ),
    SuccessCase(
        "image",
        ("media",),
        frozenset({"image", "request", "provider_unit"}),
    ),
    SuccessCase(
        "video",
        ("media",),
        frozenset({"video_second", "request", "provider_unit"}),
    ),
    SuccessCase(
        "audio",
        ("media",),
        frozenset({"audio_second", "request", "provider_unit"}),
    ),
)


async def capture_attempt(
    adapter: ProviderAdapter, request: ProviderAttemptRequest
) -> AttemptCapture:
    """Collect one network-independent adapter attempt without hiding its failure."""
    events: list[ProviderOutput | ProviderCompleted] = []
    try:
        async for event in adapter.attempt(request):
            # Keep events that occurred before an adapter failure.
            events.append(event)  # noqa: PERF401
    except ProviderFailureError as error:
        return AttemptCapture(tuple(events), error)
    return AttemptCapture(tuple(events), None)


async def assert_success_suite(
    adapter: ProviderAdapter,
    request_factory: Callable[[str], ProviderAttemptRequest],
    *,
    priced_usage_units: frozenset[str],
    cases: Sequence[SuccessCase] = SUCCESS_CASES,
) -> None:
    """Run every named operation so one incomplete adapter cannot pass by omission."""
    names = [case.name for case in cases]
    assert names
    assert len(names) == len(set(names))
    for case in cases:
        request = request_factory(case.name)
        capture = await capture_attempt(adapter, request)
        assert_success(
            adapter,
            request,
            capture,
            case,
            priced_usage_units=priced_usage_units,
        )


def assert_success(
    adapter: ProviderAdapter,
    request: ProviderAttemptRequest,
    capture: AttemptCapture,
    case: SuccessCase,
    *,
    priced_usage_units: frozenset[str],
) -> None:
    """Check ordering, terminal completion, usage declaration, and price coverage."""
    assert capture.failure is None
    assert capture.events
    assert isinstance(capture.events[-1], ProviderCompleted)
    assert all(
        not isinstance(event, ProviderCompleted) for event in capture.events[:-1]
    )
    outputs = tuple(
        event for event in capture.events if isinstance(event, ProviderOutput)
    )
    assert tuple(output.kind for output in outputs) == case.expected_output_kinds
    _assert_operation_outputs(request, outputs, case)
    for output in outputs:
        if output.kind == "media":
            value = json.loads(output.content_json)
            assert isinstance(value, dict)
            assert output.media_body is not None
            assert len(output.media_body) == value["size_bytes"]
        else:
            assert output.media_body is None
    completion = capture.events[-1]
    assert isinstance(completion, ProviderCompleted)
    units = tuple(item.unit for item in completion.usage)
    assert len(units) == len(set(units))
    assert frozenset(units) == case.expected_usage_units
    declared = adapter.usage_units_for(request.operation)
    assert declared == case.expected_usage_units
    assert frozenset(units) <= declared <= adapter.usage_units
    assert declared <= priced_usage_units
    assert all(
        item.quantity.is_finite() and item.quantity >= 0 for item in completion.usage
    )


def assert_failure(
    adapter: ProviderAdapter,
    request: ProviderAttemptRequest,
    capture: AttemptCapture,
    expected: FailureCase,
    *,
    priced_usage_units: frozenset[str],
) -> None:
    """Check one safe failure, visibility boundary, and partial usage declaration."""
    assert capture.failure is not None
    assert capture.failure.failure_class == expected.failure_class
    assert capture.failure.phase is expected.phase
    assert str(capture.failure) == "The provider attempt failed."
    visible = any(
        isinstance(event, ProviderOutput) and event.kind in {"text_delta", "tool_call"}
        for event in capture.events
    )
    assert visible is expected.visible_before_failure
    assert all(not isinstance(event, ProviderCompleted) for event in capture.events)
    units = tuple(item.unit for item in capture.failure.usage)
    assert len(units) == len(set(units))
    declared = adapter.usage_units_for(request.operation)
    assert frozenset(units) <= declared <= adapter.usage_units
    assert declared <= priced_usage_units
    assert all(
        item.quantity.is_finite() and item.quantity >= 0
        for item in capture.failure.usage
    )


def _assert_operation_outputs(
    request: ProviderAttemptRequest,
    outputs: Sequence[ProviderOutput],
    case: SuccessCase,
) -> None:
    values = [_load_finite_json(output.content_json) for output in outputs]
    if case.name in {"standard_text", "input_image"}:
        assert len(values) == 1
        _assert_standard_content(values[0], expect_tools=False)
        if case.name == "input_image":
            assert {item.media_type for item in request.input_media} == {
                "image/jpeg",
                "image/png",
                "image/webp",
            }
            assert all(
                type(item.body) is bytes and item.body for item in request.input_media
            )
        return
    if case.name == "stream_text":
        assert values
        assert all(isinstance(value, str) and value for value in values)
        return
    if case.name in {"buffered_tools", "stream_tools"}:
        tools = values[0] if case.name == "buffered_tools" else values
        assert isinstance(tools, list)
        _assert_tool_calls(tools)
        body = _load_finite_json(request.request_json)
        assert isinstance(body, dict)
        definitions = body.get("tools")
        assert isinstance(definitions, list)
        assert [tool["name"] for tool in tools] == [
            item["name"] for item in definitions
        ]
        return
    if case.name == "structured_json":
        assert len(values) == 1
        assert isinstance(values[0], dict)
        return
    if case.name == "embedding":
        assert len(values) == 1
        assert isinstance(values[0], list)
        vectors = values[0]
        assert len(vectors) == request.expected_embedding_count
        dimension = request.requirements.embedding_dimension
        assert all(
            isinstance(vector, list)
            and len(vector) == dimension
            and all(_finite_number(item) for item in vector)
            for vector in vectors
        )
        return
    if case.name in {"image", "video", "audio"}:
        assert len(values) == 1
        assert isinstance(values[0], dict)
        metadata = values[0]
        media_types = {
            "image": {"image/jpeg", "image/png", "image/webp"},
            "video": {"video/mp4", "video/webm"},
            "audio": {"audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav"},
        }
        assert set(metadata) == {"media_type", "size_bytes"}
        assert isinstance(metadata["media_type"], str)
        assert metadata["media_type"] in media_types[case.name]
        assert type(metadata["size_bytes"]) is int
        assert metadata["size_bytes"] > 0
        assert type(outputs[0].media_body) is bytes
        assert len(outputs[0].media_body) == metadata["size_bytes"]
        return
    message = f"The conformance fixture has no semantic check for {case.name}."
    raise AssertionError(message)


def _assert_standard_content(value: object, *, expect_tools: bool) -> None:
    assert isinstance(value, list)
    assert value
    if expect_tools:
        _assert_tool_calls(value)
        return
    assert all(
        isinstance(item, dict)
        and set(item) == {"type", "text"}
        and item["type"] == "text"
        and isinstance(item["text"], str)
        for item in value
    )


def _assert_tool_calls(values: Sequence[object]) -> None:
    ids: list[str] = []
    for value in values:
        assert isinstance(value, dict)
        assert set(value) == {"type", "id", "name", "arguments_json"}
        assert value["type"] == "tool_call"
        assert all(
            isinstance(value[field], str) and value[field]
            for field in ("id", "name", "arguments_json")
        )
        arguments = _load_finite_json(value["arguments_json"])
        assert isinstance(arguments, dict)
        ids.append(value["id"])
    assert len(ids) == len(set(ids))


def _load_finite_json(value: str) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError

    parsed = json.loads(value, parse_constant=reject_constant)
    assert _finite_json(parsed)
    return parsed


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


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
    )


def assert_cumulative_output_bound(
    events: Sequence[ProviderOutput | ProviderCompleted], maximum_bytes: int
) -> None:
    """Fail when the complete adapter output crosses one configured byte bound."""
    used = sum(
        len(event.content_json.encode("utf-8"))
        for event in events
        if isinstance(event, ProviderOutput)
    )
    if used > maximum_bytes:
        message = "The adapter output exceeds the cumulative byte bound."
        raise AssertionError(message)

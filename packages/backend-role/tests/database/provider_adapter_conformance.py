"""Common assertions for each applicable native provider-adapter operation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from llmrouter_backend.calls import (
    ProviderAdapter,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureError,
    ProviderOutput,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


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
    declared = adapter.usage_units_for(request)
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
    visible = any(
        isinstance(event, ProviderOutput) and event.kind in {"text_delta", "tool_call"}
        for event in capture.events
    )
    assert visible is expected.visible_before_failure
    assert all(not isinstance(event, ProviderCompleted) for event in capture.events)
    units = tuple(item.unit for item in capture.failure.usage)
    assert len(units) == len(set(units))
    declared = adapter.usage_units_for(request)
    assert frozenset(units) <= declared <= adapter.usage_units
    assert declared <= priced_usage_units
    assert all(
        item.quantity.is_finite() and item.quantity >= 0
        for item in capture.failure.usage
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

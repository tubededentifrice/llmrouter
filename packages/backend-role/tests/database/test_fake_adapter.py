"""Deterministic fake adapter and common provider conformance tests."""
# ruff: noqa: PLR0911, PLR0913

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from dataclasses import FrozenInstanceError, fields, replace
from typing import TYPE_CHECKING, Any, cast

import llmrouter_backend.adapters.fake as fake_module
import pytest
from llmrouter_backend.adapters.fake import FakeAdapter
from llmrouter_backend.calls import (
    CallKind,
    CallRequirements,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOperation,
    ProviderOutput,
)
from llmrouter_backend.catalog import ProviderRoute
from llmrouter_backend.diagnostics import CapturedMedia
from llmrouter_backend.models import ModelConstraints
from opendle import CallFailurePhase

from .provider_adapter_conformance import (
    SUCCESS_CASES,
    FailureCase,
    SuccessCase,
    assert_cumulative_output_bound,
    assert_failure,
    assert_success,
    assert_success_suite,
    capture_attempt,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_PRICED_UNITS = frozenset(
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


def _route(provider_model_name: str) -> ProviderRoute:
    return ProviderRoute(
        provider_model_api_name="fake-model",
        provider_connection_api_name="fake-provider",
        adapter="fake",
        endpoint=None,
        provider_model_name=provider_model_name,
        credential_api_name=None,
        constraints=ModelConstraints(),
        reasoning_level=None,
        provider_reasoning_value=None,
    )


def _attempt(
    provider_model_name: str,
    *,
    kind: CallKind = "model",
    output: str = "text",
    capabilities: frozenset[str] = frozenset(),
    streaming: bool = False,
    body: Mapping[str, object] | None = None,
    embedding_dimension: int | None = None,
    embedding_count: int | None = None,
    media: tuple[CapturedMedia, ...] = (),
    duration: int | None = None,
    credential: str | None = None,
) -> ProviderAttemptRequest:
    inputs = frozenset({"text", "image"}) if media else frozenset({"text"})
    return ProviderAttemptRequest(
        route=_route(provider_model_name),
        request_json=json.dumps(
            body if body is not None else {"messages": [{"role": "user"}]},
            separators=(",", ":"),
        ),
        credential=credential,
        kind=kind,
        requirements=CallRequirements(
            inputs,
            output,
            capabilities,
            embedding_dimension=embedding_dimension,
            input_image_sizes=tuple(len(item.body) for item in media),
            output_duration_seconds=duration,
        ),
        streaming=streaming,
        expected_embedding_count=embedding_count,
        input_media=media,
    )


def _success_request(name: str) -> ProviderAttemptRequest:
    tools = [
        {"name": "first", "description": "First.", "input_schema_json": "{}"},
        {"name": "second", "description": "Second.", "input_schema_json": "{}"},
    ]
    if name == "standard_text":
        return _attempt("fake-text-v1")
    if name == "stream_text":
        return _attempt(
            "fake-text-v1",
            streaming=True,
            capabilities=frozenset({"streaming"}),
        )
    if name in {"buffered_tools", "stream_tools"}:
        streaming = name == "stream_tools"
        capabilities = frozenset(
            {"tool_calling", "streaming"} if streaming else {"tool_calling"}
        )
        return _attempt(
            "fake-text-v1",
            streaming=streaming,
            capabilities=capabilities,
            body={"messages": [{"role": "user"}], "tools": tools},
        )
    if name == "structured_json":
        return _attempt(
            "fake-text-v1",
            output="structured_json",
            body={
                "messages": [{"role": "user"}],
                "output_format": {
                    "type": "json_schema",
                    "schema_json": '{"type":"object"}',
                },
            },
        )
    if name == "input_image":
        images = (
            CapturedMedia(b"safe jpeg bytes", "image/jpeg", "input"),
            CapturedMedia(b"safe png bytes", "image/png", "input"),
            CapturedMedia(b"safe webp bytes", "image/webp", "input"),
        )
        return _attempt(
            "fake-text-v1",
            media=images,
            body={"messages": [{"role": "user"}]},
        )
    if name == "embedding":
        return _attempt(
            "fake-embedding-v1",
            kind="embedding",
            output="embedding",
            embedding_dimension=3,
            embedding_count=2,
            body={"inputs": ["one", "two"]},
        )
    if name in {"image", "video", "audio"}:
        return _attempt(
            "fake-media-v1",
            kind="media",
            output=name,
            duration=5 if name in {"video", "audio"} else None,
            body={"kind": name, "prompt": "Create safe media."},
        )
    message = f"Unknown conformance case: {name}"
    raise AssertionError(message)


@pytest.mark.parametrize("case", SUCCESS_CASES, ids=lambda case: case.name)
def test_fake_adapter_passes_each_common_success_case(case: SuccessCase) -> None:
    """Use one fixture for each standard, stream, tool, JSON, image, and media case."""
    adapter = FakeAdapter()
    request = _success_request(case.name)
    first = asyncio.run(capture_attempt(adapter, request))
    second = asyncio.run(capture_attempt(adapter, request))

    assert first == second
    assert_success(adapter, request, first, case, priced_usage_units=_PRICED_UNITS)


def test_fake_adapter_passes_the_complete_common_success_suite() -> None:
    """Run the full reusable matrix through one entry point without omitted cases."""
    asyncio.run(
        assert_success_suite(
            FakeAdapter(),
            _success_request,
            priced_usage_units=_PRICED_UNITS,
        )
    )


@pytest.mark.parametrize(
    ("name", "outputs"),
    [
        ("standard_text", (ProviderOutput("standard", "[]"),)),
        (
            "stream_text",
            (
                ProviderOutput("text_delta", '""'),
                ProviderOutput("text_delta", '""'),
            ),
        ),
        (
            "buffered_tools",
            (
                ProviderOutput(
                    "standard",
                    '[{"type":"tool_call","id":"same","name":"first",'
                    '"arguments_json":"{}"},{"type":"tool_call","id":"same",'
                    '"name":"second","arguments_json":"{}"}]',
                ),
            ),
        ),
        (
            "stream_tools",
            (
                ProviderOutput(
                    "tool_call",
                    '{"type":"tool_call","id":"same","name":"first",'
                    '"arguments_json":"{}"}',
                ),
                ProviderOutput(
                    "tool_call",
                    '{"type":"tool_call","id":"same","name":"second",'
                    '"arguments_json":"{}"}',
                ),
            ),
        ),
        ("structured_json", (ProviderOutput("structured_json", "[]"),)),
        ("embedding", (ProviderOutput("embedding", "[[1]]"),)),
        (
            "image",
            (
                ProviderOutput(
                    "media",
                    '{"media_type":"image/gif","size_bytes":1}',
                    b"x",
                ),
            ),
        ),
    ],
)
def test_common_fixture_rejects_incomplete_operation_results(
    name: str, outputs: tuple[ProviderOutput, ...]
) -> None:
    """Reject each malformed result family even when event names are correct."""
    request = _success_request(name)
    adapter = FakeAdapter()
    good = asyncio.run(capture_attempt(adapter, request))
    completion = good.events[-1]
    assert isinstance(completion, ProviderCompleted)
    broken = replace(good, events=(*outputs, completion))
    case = next(item for item in SUCCESS_CASES if item.name == name)

    with pytest.raises(AssertionError):
        assert_success(
            adapter,
            request,
            broken,
            case,
            priced_usage_units=_PRICED_UNITS,
        )


def test_fake_adapter_has_no_clock_random_secret_or_network_dependency() -> None:
    """Prove that the fake source imports no environment or external-I/O facility."""
    tree = ast.parse(inspect.getsource(fake_module))
    imported_roots = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_roots.isdisjoint(
        {
            "datetime",
            "httpx",
            "os",
            "random",
            "requests",
            "secrets",
            "socket",
            "time",
            "urllib",
        }
    )


def test_fake_outputs_are_deterministic_ordered_and_secret_free() -> None:
    """Repeat one tool call exactly and do not copy model or control secrets."""
    adapter = FakeAdapter()
    request = _success_request("stream_tools")
    private_model_content = "private-model-content"
    private_control = "private-control-secret"
    request = replace(
        request,
        request_json=request.request_json.replace("user", private_model_content),
    )
    first = asyncio.run(capture_attempt(adapter, request))
    second = asyncio.run(capture_attempt(adapter, request))
    serialized = repr((first, second))

    assert first == second
    assert [
        event.kind for event in first.events if isinstance(event, ProviderOutput)
    ] == [
        "tool_call",
        "tool_call",
    ]
    assert "fake-call-001" in serialized
    assert "fake-call-002" in serialized
    assert private_model_content not in serialized
    assert private_control not in serialized

    denied = asyncio.run(
        capture_attempt(adapter, replace(request, credential=private_control))
    )
    assert_failure(
        adapter,
        request,
        denied,
        FailureCase("authentication", visible_before_failure=False),
        priced_usage_units=_PRICED_UNITS,
    )
    assert private_control not in str(denied.failure)


@pytest.mark.parametrize(
    ("model", "failure_class"),
    [
        ("fake-error-authentication-v1", "authentication"),
        ("fake-error-rate-v1", "rate_limited"),
        ("fake-error-timeout-v1", "timeout"),
        ("fake-error-transport-v1", "transport"),
        ("fake-error-unavailable-v1", "unavailable"),
        ("fake-error-refusal-v1", "refusal"),
        ("fake-error-incompatible-v1", "incompatible"),
        ("fake-error-invalid-response-v1", "invalid_response"),
        ("fake-error-interrupted-v1", "interrupted"),
        ("fake-error-upstream-v1", "upstream_failed"),
    ],
)
def test_fake_adapter_normalizes_each_failure_before_visible_output(
    model: str, failure_class: str
) -> None:
    """Return each safe provider failure without provider text or visible output."""
    adapter = FakeAdapter()
    request = _attempt(model)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase(failure_class, visible_before_failure=False),
        priced_usage_units=_PRICED_UNITS,
    )
    assert str(capture.failure) == "The provider attempt failed."


def test_fake_adapter_marks_after_visible_and_uncertain_media_failures() -> None:
    """Keep the exact no-fallback boundary for stream and media side effects."""
    adapter = FakeAdapter()
    interrupted_request = _attempt(
        "fake-stream-interruption-v1",
        streaming=True,
        capabilities=frozenset({"streaming"}),
    )
    interrupted = asyncio.run(
        capture_attempt(
            adapter,
            interrupted_request,
        )
    )
    uncertain_request = _attempt(
        "fake-media-uncertain-v1",
        kind="media",
        output="image",
        body={"kind": "image", "prompt": "Create it."},
    )
    uncertain = asyncio.run(
        capture_attempt(
            adapter,
            uncertain_request,
        )
    )

    assert_failure(
        adapter,
        interrupted_request,
        interrupted,
        FailureCase("interrupted", visible_before_failure=True),
        priced_usage_units=_PRICED_UNITS,
    )
    assert_failure(
        adapter,
        uncertain_request,
        uncertain,
        FailureCase(
            "transport",
            visible_before_failure=False,
            phase=CallFailurePhase.UNCERTAIN,
        ),
        priced_usage_units=_PRICED_UNITS,
    )
    assert uncertain.failure is not None
    assert uncertain.failure.phase is CallFailurePhase.UNCERTAIN


@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "stream"])
def test_fake_adapter_can_supply_duplicate_tool_id_invalid_responses(
    *, streaming: bool
) -> None:
    """Make one stable invalid provider response for core conformance checks."""
    capabilities = frozenset(
        {"tool_calling", "streaming"} if streaming else {"tool_calling"}
    )
    capture = asyncio.run(
        capture_attempt(
            FakeAdapter(),
            _attempt(
                "fake-duplicate-tool-ids-v1",
                streaming=streaming,
                capabilities=capabilities,
            ),
        )
    )
    outputs = [
        json.loads(event.content_json)
        for event in capture.events
        if isinstance(event, ProviderOutput)
    ]
    ids = (
        [item["id"] for item in outputs]
        if streaming
        else [item["id"] for item in outputs[0]]
    )

    assert ids == ["duplicate", "duplicate"]


def test_fake_adapter_cancellation_has_no_late_result_or_changed_repeat() -> None:
    """Stop one waiting attempt and keep later deterministic calls independent."""

    async def run_case() -> None:
        adapter = FakeAdapter()
        task = asyncio.create_task(capture_attempt(adapter, _attempt("fake-cancel-v1")))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        capture = await capture_attempt(adapter, _success_request("standard_text"))
        assert capture.failure is None
        assert isinstance(capture.events[-1], ProviderCompleted)

    asyncio.run(run_case())


def test_fake_waiting_attempt_obeys_the_caller_timeout() -> None:
    """Let the call boundary stop a waiting fake without a fake-owned clock."""

    async def run_case() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await capture_attempt(FakeAdapter(), _attempt("fake-cancel-v1"))

    asyncio.run(run_case())


@pytest.mark.parametrize(
    ("count", "dimension"),
    [(1, 1), (32, 1536)],
    ids=["minimum", "maximum-fake-catalog-batch"],
)
def test_fake_embedding_batch_and_dimension_bounds(count: int, dimension: int) -> None:
    """Return one finite vector in order for each bounded batch input."""
    request = _attempt(
        "fake-embedding-v1",
        kind="embedding",
        output="embedding",
        embedding_dimension=dimension,
        embedding_count=count,
        body={"inputs": [f"item-{index}" for index in range(count)]},
    )
    case = next(item for item in SUCCESS_CASES if item.name == "embedding")
    capture = asyncio.run(capture_attempt(FakeAdapter(), request))

    assert_success(
        FakeAdapter(), request, capture, case, priced_usage_units=_PRICED_UNITS
    )
    output = cast("ProviderOutput", capture.events[0])
    vectors = json.loads(output.content_json)
    assert [vector[0] for vector in vectors] == list(range(count))


def test_fake_embedding_rejects_a_body_count_mismatch() -> None:
    """Do not create missing or extra embedding results from adapter metadata."""
    request = _attempt(
        "fake-embedding-v1",
        kind="embedding",
        output="embedding",
        embedding_dimension=3,
        embedding_count=2,
        body={"inputs": ["only-one"]},
    )
    capture = asyncio.run(capture_attempt(FakeAdapter(), request))

    assert_failure(
        FakeAdapter(),
        request,
        capture,
        FailureCase("incompatible", visible_before_failure=False),
        priced_usage_units=_PRICED_UNITS,
    )


def test_common_fixture_detects_cumulative_output_bounds() -> None:
    """Count all stream events instead of checking each event in isolation."""
    capture = asyncio.run(
        capture_attempt(
            FakeAdapter(),
            _attempt(
                "fake-cumulative-output-v1",
                streaming=True,
                capabilities=frozenset({"streaming"}),
            ),
        )
    )

    assert_cumulative_output_bound(capture.events, 1_000)
    with pytest.raises(AssertionError, match="cumulative"):
        assert_cumulative_output_bound(capture.events, 100)


@pytest.mark.parametrize(
    "unsafe",
    [
        "not-json",
        '{"messages":[NaN]}',
        '{"messages":[1e999]}',
        "[" * 2_000 + "0" + "]" * 2_000,
        '{"messages":[],"agent_run":{"id":"removed"}}',
        '{"messages":[],"durable_request":true}',
        '{"messages":[],"openai_compatible":true}',
        '{"messages":[],"resume_token":"removed"}',
    ],
)
def test_fake_adapter_rejects_unsafe_and_removed_request_surfaces(unsafe: str) -> None:
    """Reject invalid JSON, non-finite numbers, and removed product controls."""
    request = replace(_attempt("fake-text-v1"), request_json=unsafe)
    adapter = FakeAdapter()
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase("incompatible", visible_before_failure=False),
        priced_usage_units=_PRICED_UNITS,
    )


def test_fake_adapter_rejects_input_and_output_bound_failures() -> None:
    """Reject mismatched images, large input, non-finite output, and unknown routes."""
    image = CapturedMedia(b"image", "image/png", "input")
    mismatched = replace(_attempt("fake-text-v1", media=(image,)), input_media=())
    too_large = replace(
        _attempt("fake-text-v1"),
        request_json=json.dumps({"messages": ["x" * (2 * 1024 * 1024)]}),
    )
    unknown = _attempt("fake-agent-v1")
    wrong_adapter = replace(
        _attempt("fake-text-v1"),
        route=replace(_route("fake-text-v1"), adapter="openai"),
    )

    adapter = FakeAdapter()
    for request in (mismatched, too_large, unknown, wrong_adapter):
        capture = asyncio.run(capture_attempt(adapter, request))
        assert_failure(
            adapter,
            request,
            capture,
            FailureCase("incompatible", visible_before_failure=False),
            priced_usage_units=_PRICED_UNITS,
        )
    with pytest.raises(ValueError, match="valid JSON"):
        ProviderOutput("embedding", "[NaN]")
    with pytest.raises(ValueError, match="valid JSON"):
        ProviderOutput("embedding", "[[1e999]]")
    with pytest.raises(ValueError, match="valid JSON"):
        ProviderOutput("embedding", "[" * 2_000 + "0" + "]" * 2_000)
    with pytest.raises(ValueError, match="safe bounds"):
        ProviderOutput("standard", json.dumps("x" * 5_000_000))
    with pytest.raises(ValueError, match="media body"):
        ProviderOutput("media", '{"media_type":"image/png","size_bytes":2}', b"one")
    with pytest.raises(ValueError, match="media body"):
        ProviderOutput(
            "media",
            '{"media_type":"image/png","size_bytes":3}',
            cast("Any", bytearray(b"one")),
        )


def test_adapter_boundary_has_only_current_call_fields_and_operations() -> None:
    """Keep removed agents, retries, recovery, and provider products out of the port."""
    assert [field.name for field in fields(ProviderAttemptRequest)] == [
        "route",
        "request_json",
        "credential",
        "kind",
        "requirements",
        "streaming",
        "expected_embedding_count",
        "input_media",
    ]
    operation = _success_request("input_image").operation
    assert isinstance(operation, ProviderOperation)
    assert [field.name for field in fields(ProviderOperation)] == [
        "kind",
        "requirements",
        "streaming",
        "expected_embedding_count",
    ]
    for control in ("credential", "request_json", "input_media", "route"):
        assert not hasattr(operation, control)
    with pytest.raises(FrozenInstanceError):
        operation.streaming = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        operation.requirements.required_output = "image"  # type: ignore[misc]
    adapter = FakeAdapter()
    for removed in (
        "run_agent",
        "execute_tool",
        "retry",
        "cancel",
        "resume",
        "replay",
        "openai_chat_completion",
    ):
        assert not hasattr(adapter, removed)

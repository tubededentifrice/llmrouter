"""Deterministic transport tests for the accepted text-provider adapters."""
# ruff: noqa: PLR0913, PLR2004

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from llmrouter_backend.adapters import OllamaTextAdapter, OpenAITextAdapter
from llmrouter_backend.calls import (
    CallRequirements,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOutput,
)
from llmrouter_backend.catalog import ProviderRoute
from llmrouter_backend.diagnostics import CapturedMedia
from llmrouter_backend.models import ModelConstraints, ProviderWrite
from opendle import CallFailurePhase

from .provider_adapter_conformance import (
    FailureCase,
    SuccessCase,
    assert_failure,
    assert_success,
    capture_attempt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from llmrouter_backend.adapters.text import OpenAIAdapterName

_SECRET = "provider-control-placeholder"  # noqa: S105  # nosec B105
_OPENAI_UNITS = frozenset(
    {"input_token", "output_token", "cached_input_token", "request"}
)
_OLLAMA_UNITS = frozenset({"input_token", "output_token", "request"})


def _route(
    adapter: str,
    *,
    endpoint: str | None = "https://provider.example/v1",
    reasoning: str | None = None,
) -> ProviderRoute:
    if adapter in {"openai", "openrouter"}:
        endpoint = None
    return ProviderRoute(
        provider_model_api_name="text-model",
        provider_connection_api_name="text-provider",
        adapter=adapter,
        endpoint=endpoint,
        provider_model_name="wire-model",
        credential_api_name="credential" if adapter != "ollama" else None,
        constraints=ModelConstraints(
            max_input_images=8, max_input_image_bytes=20_000_000
        ),
        reasoning_level="medium" if reasoning is not None else None,
        provider_reasoning_value=reasoning,
    )


def _request(
    *,
    adapter: str = "custom",
    endpoint: str | None = "https://provider.example/v1",
    body: Mapping[str, object] | None = None,
    output: str = "text",
    streaming: bool = False,
    capabilities: frozenset[str] = frozenset(),
    media: tuple[CapturedMedia, ...] = (),
    credential: str | None = _SECRET,
    reasoning: str | None = None,
) -> ProviderAttemptRequest:
    return ProviderAttemptRequest(
        route=_route(adapter, endpoint=endpoint, reasoning=reasoning),
        request_json=json.dumps(
            body
            if body is not None
            else {
                "messages": [
                    {"role": "system", "content": "Follow the request."},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello."}],
                    },
                ]
            },
            separators=(",", ":"),
        ),
        credential=credential,
        kind="model",
        requirements=CallRequirements(
            frozenset({"text", "image"} if media else {"text"}),
            output,
            capabilities,
            input_image_sizes=tuple(len(item.body) for item in media),
        ),
        streaming=streaming,
        expected_embedding_count=None,
        input_media=media,
    )


def _usage() -> dict[str, object]:
    return {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 2},
    }


def _completion(content: str = "Provider response.") -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": _usage(),
    }


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    adapter: OpenAIAdapterName = "custom",
) -> OpenAITextAdapter:
    return OpenAITextAdapter(adapter, httpx.MockTransport(handler))


def test_openai_profiles_use_only_the_exact_trusted_endpoint_and_safe_headers() -> None:
    """Use fixed vendor origins, exact custom origins, and one bearer control."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion())

    cases: tuple[tuple[OpenAIAdapterName, str], ...] = (
        ("openai", "https://api.openai.com/v1/chat/completions"),
        ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
        ("openai_compatible", "https://provider.example/v1/chat/completions"),
        ("custom", "https://provider.example/v1/chat/completions"),
    )
    for adapter_name, expected_url in cases:
        request = _request(adapter=adapter_name)
        capture = asyncio.run(capture_attempt(_adapter(handler, adapter_name), request))
        assert capture.failure is None
        assert str(seen[-1].url) == expected_url
        assert seen[-1].headers["authorization"] == f"Bearer {_SECRET}"
        assert "cookie" not in seen[-1].headers
        assert seen[-1].extensions["timeout"] == {
            "connect": 10.0,
            "read": 60.0,
            "write": 10.0,
            "pool": 10.0,
        }


def test_optional_custom_credential_omits_authorization_and_openai_uses_its_limit() -> (
    None
):
    """Do not invent auth for a no-key endpoint and use the fixed OpenAI limit field."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion())

    no_key = _request(adapter="custom", credential=None)
    openai = _request(
        adapter="openai",
        body={
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello."}],
                }
            ],
            "output_limit": 123,
        },
    )
    assert asyncio.run(capture_attempt(_adapter(handler), no_key)).failure is None
    assert (
        asyncio.run(capture_attempt(_adapter(handler, "openai"), openai)).failure
        is None
    )

    assert "authorization" not in seen[0].headers
    openai_body = json.loads(seen[1].content)
    assert openai_body["max_completion_tokens"] == 123
    assert "max_tokens" not in openai_body


def test_openrouter_uses_its_reasoning_object() -> None:
    """Put the mapped effort in the OpenRouter field, not the OpenAI field."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion())

    request = _request(
        adapter="openrouter",
        reasoning="high",
        capabilities=frozenset({"reasoning"}),
    )
    capture = asyncio.run(capture_attempt(_adapter(handler, "openrouter"), request))

    assert capture.failure is None
    assert captured["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in captured


def test_openai_maps_messages_images_tools_results_json_reasoning_and_bounds() -> None:
    """Map all accepted neutral text facts without a provider field in the API."""
    image = CapturedMedia(b"bounded-image", "image/png", "input")
    body = {
        "messages": [
            {"role": "system", "content": "System."},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Use a tool."},
                    {
                        "type": "tool_call",
                        "id": "prior-call",
                        "name": "lookup",
                        "arguments_json": '{"value":1}',
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "prior-call",
                        "result_json": '{"result":"ok"}',
                    },
                    {"type": "text", "text": "Inspect this."},
                    {"type": "image", "media_type": "image/png", "data_base64": ""},
                ],
            },
        ],
        "tools": [
            {
                "name": "lookup",
                "description": "Look up a value.",
                "input_schema_json": '{"type":"object"}',
            }
        ],
        "output_format": {
            "type": "json_schema",
            "schema_json": '{"type":"object"}',
        },
        "output_limit": 200,
        "temperature": 0.25,
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion('{"answer":true}'))

    request = _request(
        body=body,
        output="structured_json",
        capabilities=frozenset({"tool_calling", "reasoning"}),
        media=(image,),
        reasoning="high",
    )
    capture = asyncio.run(capture_attempt(_adapter(handler), request))

    assert capture.failure is None
    assert captured["model"] == "wire-model"
    assert captured["reasoning_effort"] == "high"
    assert captured["max_tokens"] == 200
    assert captured["temperature"] == 0.25
    assert captured["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "router_response",
            "strict": True,
            "schema": {"type": "object"},
        },
    }
    messages = cast("list[dict[str, object]]", captured["messages"])
    assert [item["role"] for item in messages] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    assert "bounded-image" not in repr(captured)
    assert "Ym91bmRlZC1pbWFnZQ==" in repr(captured)
    assert _SECRET not in repr(captured)
    output = cast("ProviderOutput", capture.events[0])
    assert output.kind == "structured_json"
    assert json.loads(output.content_json) == {"answer": True}


@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "stream"])
def test_openai_maps_text_and_tool_results_with_exact_usage(*, streaming: bool) -> None:
    """Map buffered or SSE tool calls and separate cached input usage."""
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Call tools."}]}
        ],
        "tools": [
            {
                "name": "first",
                "description": "First.",
                "input_schema_json": "{}",
            },
            {
                "name": "second",
                "description": "Second.",
                "input_schema_json": "{}",
            },
        ],
    }
    if streaming:
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"name": "first", "arguments": '{"a":'},
                                },
                                {
                                    "index": 1,
                                    "id": "call-2",
                                    "function": {"name": "second", "arguments": "{}"},
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "1}"}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": _usage()},
        ]
        wire = (
            b"".join(
                b"data: " + json.dumps(item, separators=(",", ":")).encode() + b"\n\n"
                for item in chunks
            )
            + b"data: [DONE]\n\n"
        )
        response = httpx.Response(
            200, content=wire, headers={"Content-Type": "text/event-stream"}
        )
    else:
        response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "first",
                                        "arguments": '{"a":1}',
                                    },
                                },
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {"name": "second", "arguments": "{}"},
                                },
                            ],
                        },
                    }
                ],
                "usage": _usage(),
            },
        )

    request = _request(
        body=body,
        streaming=streaming,
        capabilities=frozenset(
            {"tool_calling", "streaming"} if streaming else {"tool_calling"}
        ),
    )
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))
    expected = SuccessCase(
        "stream_tools" if streaming else "buffered_tools",
        ("tool_call", "tool_call") if streaming else ("standard",),
        _OPENAI_UNITS,
    )

    assert_success(
        _adapter(lambda _request: response),
        request,
        capture,
        expected,
        priced_usage_units=_OPENAI_UNITS,
    )
    completion = cast("ProviderCompleted", capture.events[-1])
    assert {item.unit: item.quantity for item in completion.usage} == {
        "request": 1,
        "input_token": 10,
        "cached_input_token": 2,
        "output_token": 3,
    }


def test_openai_sse_emits_visible_text_before_a_safe_interruption() -> None:
    """Keep released output and normalize a later truncated provider stream."""
    wire = (
        b'data: {"choices":[{"delta":{"content":"visible"},"finish_reason":null}]}\n\n'
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(streaming=True, capabilities=frozenset({"streaming"}))
    adapter = _adapter(lambda _request: response)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase("invalid_response", visible_before_failure=True),
        priced_usage_units=_OPENAI_UNITS,
    )
    assert str(capture.failure) == "The provider attempt failed."
    assert capture.failure is not None
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1
    }


def test_openai_stream_keeps_reported_usage_when_later_data_is_invalid() -> None:
    """Keep billable provider facts when a later stream event cannot be accepted."""
    wire = (
        b'data: {"choices":[{"delta":{"content":"visible"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":12,'
        b'"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}}}\n\n'
        b'data: {"choices":"invalid"}\n\n'
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(streaming=True, capabilities=frozenset({"streaming"}))
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 10,
        "cached_input_token": 2,
        "output_token": 3,
    }


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (401, "authentication"),
        (429, "rate_limited"),
        (408, "timeout"),
        (400, "incompatible"),
        (503, "unavailable"),
        (302, "invalid_response"),
    ],
)
def test_openai_normalizes_http_failures_without_provider_text(
    status: int, failure: str
) -> None:
    """Do not return a provider body, location, credential, or product error."""
    response = httpx.Response(
        status,
        text=f"private provider failure {_SECRET}",
        headers={"Location": "https://redirect.example/private"},
    )
    request = _request()
    adapter = _adapter(lambda _request: response)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase(failure, visible_before_failure=False),
        priced_usage_units=_OPENAI_UNITS,
    )
    assert _SECRET not in str(capture.failure)
    assert "redirect" not in str(capture.failure)
    assert capture.failure is not None
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://provider.example/v1",
        "https://127.0.0.2/v1",
        "https://10.0.0.1/v1",
        "https://provider.example/v1?secret=value",
        "https://user:pass@provider.example/v1",
    ],
)
def test_custom_endpoint_revalidates_exact_transport_trust(endpoint: str) -> None:
    """Fail before transport when a route snapshot has an unsafe endpoint."""
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_completion())

    request = _request(endpoint=endpoint)
    adapter = _adapter(handler)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase("incompatible", visible_before_failure=False),
        priced_usage_units=_OPENAI_UNITS,
    )
    assert not called


def test_provider_credential_cannot_inject_or_create_an_authorization_header() -> None:
    """Reject a non-bearer control before transport and do not expose its value."""
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_completion())

    unsafe_control = "credential\r\nX-Unsafe: value"
    request = replace(_request(), credential=unsafe_control)
    adapter = _adapter(handler)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase("authentication", visible_before_failure=False),
        priced_usage_units=_OPENAI_UNITS,
    )
    assert not called
    assert unsafe_control not in str(capture.failure)

    ollama_request = _request(
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        credential="",
    )
    ollama_capture = asyncio.run(
        capture_attempt(OllamaTextAdapter(httpx.MockTransport(handler)), ollama_request)
    )
    assert_failure(
        OllamaTextAdapter(httpx.MockTransport(handler)),
        ollama_request,
        ollama_capture,
        FailureCase("authentication", visible_before_failure=False),
        priced_usage_units=_OLLAMA_UNITS,
    )
    assert not called


@pytest.mark.parametrize(
    "payload",
    [
        b'{"choices":[{"finish_reason":"stop","message":{"content":NaN}}]}',
        b'{"choices":[]}',
        b'{"choices":[{"finish_reason":"stop","message":{"content":""}}]}',
    ],
)
def test_openai_rejects_nonfinite_empty_and_invalid_responses(payload: bytes) -> None:
    """Reject invalid provider output before it enters a neutral result."""
    response = httpx.Response(
        200, content=payload, headers={"Content-Type": "application/json"}
    )
    request = _request()
    adapter = _adapter(lambda _request: response)
    capture = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        capture,
        FailureCase("invalid_response", visible_before_failure=False),
        priced_usage_units=_OPENAI_UNITS,
    )
    assert capture.failure is not None
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1
    }


def test_openai_rejects_too_many_buffered_tools_and_keeps_usage() -> None:
    """Apply the 128-tool bound and keep usage from the rejected response."""
    calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        for index in range(129)
    ]
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": calls,
                    },
                }
            ],
            "usage": _usage(),
        },
    )
    request = _request(capabilities=frozenset({"tool_calling"}))
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert not capture.events
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 10,
        "cached_input_token": 2,
        "output_token": 3,
    }


def test_openai_rejects_an_oversized_unterminated_sse_event_before_visibility() -> None:
    """Apply the event bound at end of file before one delta becomes visible."""
    content = "x" * (1024 * 1024)
    wire = (
        b'data: {"choices":[{"delta":{"content":'
        + json.dumps(content).encode()
        + b'},"finish_reason":null}]}'
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(streaming=True, capabilities=frozenset({"streaming"}))
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert not capture.events
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1
    }


def test_openai_stream_rejects_unrequested_tools_and_keeps_terminal_usage() -> None:
    """Do not release one provider tool call that the native call did not request."""
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": _usage()},
    ]
    wire = (
        b"".join(
            b"data: " + json.dumps(item, separators=(",", ":")).encode() + b"\n\n"
            for item in chunks
        )
        + b"data: [DONE]\n\n"
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(streaming=True, capabilities=frozenset({"streaming"}))
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert not capture.events
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 10,
        "cached_input_token": 2,
        "output_token": 3,
    }


@pytest.mark.parametrize("invalid_kind", ["incomplete_tool", "duplicate_usage"])
def test_openai_stream_normalizes_invalid_terminal_facts(invalid_kind: str) -> None:
    """Return one safe failure for invalid facts found at stream completion."""
    if invalid_kind == "incomplete_tool":
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": _usage()},
        ]
    else:
        chunks = [
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": _usage(),
            },
            {"choices": [], "usage": _usage()},
        ]
    wire = (
        b"".join(
            b"data: " + json.dumps(item, separators=(",", ":")).encode() + b"\n\n"
            for item in chunks
        )
        + b"data: [DONE]\n\n"
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(
        streaming=True,
        capabilities=frozenset({"streaming", "tool_calling"}),
    )
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert str(capture.failure) == "The provider attempt failed."
    assert not capture.events
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 10,
        "cached_input_token": 2,
        "output_token": 3,
    }


def test_openai_stream_maps_one_structured_provider_error_without_its_text() -> None:
    """Use the safe status class and do not return private stream error text."""
    wire = (
        b'data: {"error":{"code":429,"message":"private provider detail"}}\n\n'
        b"data: [DONE]\n\n"
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(streaming=True, capabilities=frozenset({"streaming"}))
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "rate_limited"
    assert str(capture.failure) == "The provider attempt failed."
    assert "private" not in str(capture.failure)
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1
    }


def test_ollama_maps_native_buffered_and_streaming_calls() -> None:
    """Use loopback native chat, optional auth, reasoning, tools, and NDJSON."""
    requests: list[dict[str, object]] = []
    replies = [
        httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "Local response."},
                "done": True,
                "prompt_eval_count": 4,
                "eval_count": 2,
            },
        ),
        httpx.Response(
            200,
            content=(
                b'{"message":{"content":"Local "},"done":false}\n'
                b'{"message":{"content":"stream."},"done":true,'
                b'"prompt_eval_count":4,"eval_count":2}\n'
            ),
            headers={"Content-Type": "application/x-ndjson"},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return replies[len(requests) - 1]

    adapter = OllamaTextAdapter(httpx.MockTransport(handler))
    native_body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "prior",
                        "name": "lookup",
                        "arguments_json": '{"value":1}',
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "prior",
                        "result_json": '{"result":"ok"}',
                    }
                ],
            },
        ]
    }
    buffered = _request(
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        credential=None,
        reasoning="true",
        capabilities=frozenset({"tool_calling"}),
        body=native_body,
    )
    streamed = replace(
        buffered,
        streaming=True,
        requirements=replace(
            buffered.requirements,
            required_capabilities=frozenset({"tool_calling", "streaming"}),
        ),
    )
    buffered_capture = asyncio.run(capture_attempt(adapter, buffered))
    stream_capture = asyncio.run(capture_attempt(adapter, streamed))

    assert_success(
        adapter,
        buffered,
        buffered_capture,
        SuccessCase("standard_text", ("standard",), _OLLAMA_UNITS),
        priced_usage_units=_OLLAMA_UNITS,
    )
    assert_success(
        adapter,
        streamed,
        stream_capture,
        SuccessCase("stream_text", ("text_delta", "text_delta"), _OLLAMA_UNITS),
        priced_usage_units=_OLLAMA_UNITS,
    )
    assert requests[0]["think"] == "true"
    assert requests[0]["stream"] is False
    assert requests[1]["stream"] is True
    prior = cast("list[dict[str, object]]", requests[0]["messages"])[0]
    tool_call = cast("list[dict[str, object]]", prior["tool_calls"])[0]
    function = cast("dict[str, object]", tool_call["function"])
    assert function["arguments"] == {"value": 1}


def test_ollama_maps_images_structured_json_and_streamed_tool_calls() -> None:
    """Map Ollama-only image, schema, and synthetic tool-identity shapes."""
    requests: list[dict[str, object]] = []
    replies = [
        httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": '{"ok":true}'},
                "done": True,
                "prompt_eval_count": 4,
                "eval_count": 2,
            },
        ),
        httpx.Response(
            200,
            content=(
                b'{"message":{"content":"","tool_calls":[{"function":'
                b'{"name":"first","arguments":{"a":1}}}]},"done":false}\n'
                b'{"message":{"content":"","tool_calls":[{"function":'
                b'{"name":"second","arguments":{}}}]},"done":true,'
                b'"prompt_eval_count":4,"eval_count":2}\n'
            ),
            headers={"Content-Type": "application/x-ndjson"},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return replies[len(requests) - 1]

    adapter = OllamaTextAdapter(httpx.MockTransport(handler))
    image = CapturedMedia(b"bounded-image", "image/png", "input")
    structured = _request(
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        credential=None,
        output="structured_json",
        media=(image,),
        body={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect."},
                        {"type": "image", "media_type": "image/png", "data_base64": ""},
                    ],
                }
            ],
            "output_format": {
                "type": "json_schema",
                "schema_json": '{"type":"object"}',
            },
        },
    )
    tools = _request(
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        credential=None,
        streaming=True,
        capabilities=frozenset({"streaming", "tool_calling"}),
    )

    structured_capture = asyncio.run(capture_attempt(adapter, structured))
    tools_capture = asyncio.run(capture_attempt(adapter, tools))

    assert structured_capture.failure is None
    output = cast("ProviderOutput", structured_capture.events[0])
    assert output.kind == "structured_json"
    assert json.loads(output.content_json) == {"ok": True}
    assert requests[0]["format"] == {"type": "object"}
    first_message = cast("list[dict[str, object]]", requests[0]["messages"])[0]
    assert first_message["images"] == ["Ym91bmRlZC1pbWFnZQ=="]

    assert tools_capture.failure is None
    tool_outputs = [
        event for event in tools_capture.events if isinstance(event, ProviderOutput)
    ]
    assert [event.kind for event in tool_outputs] == ["tool_call", "tool_call"]
    assert [json.loads(event.content_json)["id"] for event in tool_outputs] == [
        "ollama-1",
        "ollama-2",
    ]


def test_ollama_stream_keeps_terminal_usage_when_a_late_row_is_invalid() -> None:
    """Validate the complete body before completion and keep reported usage."""
    response = httpx.Response(
        200,
        content=(
            b'{"message":{"content":"visible"},"done":true,'
            b'"prompt_eval_count":4,"eval_count":2}\n'
            b'{"message":{"content":"late"},"done":false}\n'
        ),
        headers={"Content-Type": "application/x-ndjson"},
    )
    request = _request(
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        credential=None,
        streaming=True,
        capabilities=frozenset({"streaming"}),
    )
    capture = asyncio.run(
        capture_attempt(
            OllamaTextAdapter(httpx.MockTransport(lambda _request: response)), request
        )
    )

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert {item.unit: item.quantity for item in capture.failure.usage} == {
        "request": 1,
        "input_token": 4,
        "output_token": 2,
    }
    assert not any(isinstance(event, ProviderCompleted) for event in capture.events)


@pytest.mark.parametrize("index", [-1, 128], ids=["negative", "too-many"])
def test_openai_stream_rejects_out_of_bound_tool_accumulation(index: int) -> None:
    """Do not allocate streamed tool state outside the native 128-tool bound."""
    wire = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":'
        + str(index).encode()
        + b',"id":"call","function":{"name":"tool","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
    )
    response = httpx.Response(
        200, content=wire, headers={"Content-Type": "text/event-stream"}
    )
    request = _request(
        streaming=True, capabilities=frozenset({"tool_calling", "streaming"})
    )
    capture = asyncio.run(capture_attempt(_adapter(lambda _request: response), request))

    assert capture.failure is not None
    assert capture.failure.failure_class == "invalid_response"
    assert not capture.events


def test_ollama_allows_trusted_https_and_rejects_plain_external_http() -> None:
    """Allow standard HTTPS and keep plain HTTP on explicit loopback only."""
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(503)

    adapter = OllamaTextAdapter(httpx.MockTransport(handler))
    trusted = _request(
        adapter="ollama", endpoint="https://provider.example", credential=None
    )
    trusted_capture = asyncio.run(capture_attempt(adapter, trusted))
    assert trusted_capture.failure is not None
    assert trusted_capture.failure.failure_class == "unavailable"
    assert {item.unit: item.quantity for item in trusted_capture.failure.usage} == {
        "request": 1
    }
    assert called

    called = False
    denied = _request(
        adapter="ollama", endpoint="http://provider.example", credential=None
    )
    denied_capture = asyncio.run(capture_attempt(adapter, denied))
    assert denied_capture.failure is not None
    assert denied_capture.failure.failure_class == "incompatible"
    assert denied_capture.failure.phase is CallFailurePhase.BEFORE_VISIBLE_OUTPUT
    assert not called


def test_removed_provider_and_compatibility_surfaces_are_not_registered() -> None:
    """Reject deleted native, subscription, and public compatibility products."""
    schema = ProviderWrite.model_json_schema()
    serialized = json.dumps(schema, sort_keys=True).lower()

    for removed in (
        "anthropic",
        "z.ai",
        "zai",
        "chatgpt",
        "codex",
        "subscription",
        "openai-compatible api",
    ):
        assert removed not in serialized

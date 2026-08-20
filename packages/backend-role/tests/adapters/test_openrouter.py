"""Deterministic loopback tests for the internal OpenRouter text adapter."""
# ruff: noqa: D103, PLR2004

from __future__ import annotations

import ast
import concurrent.futures
import gzip
import inspect
import json
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import pytest
from llmrouter_backend.accounting import PriceComponent, UsageUnit
from llmrouter_backend.adapters import openrouter as openrouter_module
from llmrouter_backend.adapters.model import (
    MAXIMUM_REQUEST_TEXT_BYTES,
    MessageRole,
    ModelAdapterRequest,
    ModelMessage,
    ModelOperation,
    ModelOutputEvent,
    ModelOutputEventKind,
)
from llmrouter_backend.adapters.openrouter import (
    DEEPSEEK_V4_FLASH_WIRE_MODEL,
    OPENROUTER_ADAPTER_TYPE,
    OPENROUTER_SUPPORTED_CAPABILITIES,
    OPENROUTER_UNSUPPORTED_CAPABILITIES,
    OpenRouterAdapter,
    openrouter_registered_schemas,
)
from llmrouter_backend.configuration import (
    RegisteredDocument,
    SettingsSchemaRegistry,
)
from llmrouter_backend.credential_store import SecretLease
from llmrouter_backend.execution import ErrorScope, TerminalErrorClass
from llmrouter_backend.routing import (
    AdapterPhase,
    AttemptOutcome,
    AttemptPlan,
    AttemptTimeouts,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_TEST_SECRET = b"test-openrouter-key-placeholder"


@dataclass(slots=True)
class _Reply:
    body: bytes = b""
    status: int = 200
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] = ()
    header_delay: float = 0
    chunk_delay: float = 0
    pause_after_first: threading.Event | None = None


@dataclass(frozen=True, slots=True)
class _CapturedRequest:
    path: str
    headers: MappingProxyType[str, str]
    body: bytes


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reply: _Reply) -> None:
        super().__init__(("127.0.0.1", 0), _FakeHandler)
        self.reply = reply
        self.captured: list[_CapturedRequest] = []
        self.request_seen = threading.Event()
        self.first_chunk_sent = threading.Event()


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        server = cast("_FakeServer", self.server)
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        server.captured.append(
            _CapturedRequest(
                self.path,
                MappingProxyType(
                    {key.casefold(): value for key, value in self.headers.items()}
                ),
                body,
            )
        )
        server.request_seen.set()
        reply = server.reply
        if reply.header_delay:
            time.sleep(reply.header_delay)
        with suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Connection", "close")
            for key, value in reply.headers.items():
                self.send_header(key, value)
            if not reply.chunks and "Content-Length" not in reply.headers:
                self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            if reply.chunks:
                for index, chunk in enumerate(reply.chunks):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if index == 0:
                        server.first_chunk_sent.set()
                        if reply.pause_after_first is not None:
                            reply.pause_after_first.wait(timeout=2)
                    if reply.chunk_delay:
                        time.sleep(reply.chunk_delay)
            else:
                self.wfile.write(reply.body)
                self.wfile.flush()
        self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        """Do not write request or response data to test logs."""


@contextmanager
def _server(reply: _Reply) -> Iterator[_FakeServer]:
    server = _FakeServer(reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        if reply.pause_after_first is not None:
            reply.pause_after_first.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _plan(
    endpoint: str,
    *,
    capabilities: frozenset[str] = OPENROUTER_SUPPORTED_CAPABILITIES,
    wire_model: str = DEEPSEEK_V4_FLASH_WIRE_MODEL,
    timeouts: AttemptTimeouts | None = None,
) -> AttemptPlan:
    now = datetime.now(UTC)
    return AttemptPlan(
        claim_id="claim",
        claim_generation=1,
        request_id="request",
        request_row_id="request-row",
        service_id="service",
        workspace_id="workspace",
        attempt_id="attempt",
        attempt_number=1,
        candidate_ordinal=1,
        assignment_id="assignment",
        assignment_revision="assignment-revision",
        route_snapshot_id="snapshot",
        route_snapshot_sha256=b"s" * 32,
        route_configuration_revision="route-revision",
        provider_model_route_id="route",
        route_generation=1,
        provider_instance_id="instance",
        provider_instance_generation=1,
        credential_id="credential",
        credential_generation=1,
        price_version_id="price-version",
        adapter_type=OPENROUTER_ADAPTER_TYPE,
        endpoint=endpoint,
        wire_model=wire_model,
        capabilities=capabilities,
        candidate_policy={},
        instance_settings={
            "profile": "openrouter",
            "supported_operations": [
                ModelOperation.COMPLETE.value,
                ModelOperation.STREAM.value,
            ],
            "attribution_referer": "https://router.example.test",
            "attribution_title": "LLM Router test",
        },
        route_settings={},
        typed_prices=(
            PriceComponent(
                UsageUnit.INPUT_TOKEN,
                Decimal("0.01"),
                "USD",
                "0.01",
                Decimal(1_000_000),
            ),
        ),
        timeouts=timeouts or AttemptTimeouts(500, 500, 500, 2_000),
        logical_deadline=now + timedelta(seconds=10),
        attempt_deadline=now + timedelta(seconds=2),
        diagnostic=False,
        partial_output=False,
        committed_effect=False,
        started=True,
        dispatched=True,
        recovery_only=False,
        recovery_failure=None,
        prestart_reservation_id="reservation",
        request_terminal=False,
    )


def _request(
    operation: ModelOperation = ModelOperation.COMPLETE,
) -> ModelAdapterRequest:
    return ModelAdapterRequest(
        operation,
        (ModelMessage(MessageRole.USER, "Reply with a short test value."),),
        32,
        Decimal("0.2"),
    )


@dataclass(slots=True)
class _Harness:
    request: ModelAdapterRequest
    events: list[ModelOutputEvent] = field(default_factory=list)
    leases: list[SecretLease] = field(default_factory=list)
    phases: list[AdapterPhase] = field(default_factory=list)
    credential_calls: int = 0

    def request_source(self, _plan: AttemptPlan) -> ModelAdapterRequest:
        return self.request

    def credential_source(self, _plan: AttemptPlan) -> SecretLease:
        self.credential_calls += 1
        lease = SecretLease(
            "credential",
            1,
            datetime.now(UTC) + timedelta(minutes=1),
            bytearray(_TEST_SECRET),
        )
        self.leases.append(lease)
        return lease

    def output(self, _plan: AttemptPlan, event: ModelOutputEvent) -> None:
        self.events.append(event)

    def progress(self, phase: AdapterPhase) -> None:
        self.phases.append(phase)


def _adapter(harness: _Harness) -> OpenRouterAdapter:
    return OpenRouterAdapter(
        requests=harness.request_source,
        credentials=harness.credential_source,
        output=harness.output,
        allow_loopback_test_endpoint=True,
    )


def _completion(*, usage: bool = True, content: str = "test response") -> bytes:
    value: dict[str, object] = {
        "id": "provider-id",
        "model": DEEPSEEK_V4_FLASH_WIRE_MODEL,
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        value["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 2},
            "cost": 999999,
        }
    return json.dumps(value, separators=(",", ":")).encode()


def _endpoint(server: _FakeServer) -> str:
    host = cast("str", server.server_address[0])
    port = server.server_address[1]
    return f"http://{host}:{port}/api/v1"


def test_registered_settings_are_closed_and_capabilities_are_explicit() -> None:
    registry = SettingsSchemaRegistry(openrouter_registered_schemas())
    valid = RegisteredDocument(
        "adapter.openai_compatible.settings",
        1,
        {"profile": "openrouter", "supported_operations": ["chat.complete"]},
    )
    invalid = RegisteredDocument(
        "adapter.openai_compatible.settings",
        1,
        {
            "profile": "openrouter",
            "supported_operations": ["chat.complete"],
            "api_key": "not-permitted",
        },
    )

    assert registry.validate(valid, field_path="settings") == ()
    issues = registry.validate(invalid, field_path="settings")
    assert {issue.field_path for issue in issues} == {"settings.document.api_key"}
    assert {"chat.complete", "chat.stream"} == OPENROUTER_SUPPORTED_CAPABILITIES
    assert "embedding.batch" in OPENROUTER_UNSUPPORTED_CAPABILITIES


def test_model_request_rejects_aggregate_bytes_and_invalid_unicode() -> None:
    item = ModelMessage(MessageRole.USER, "x" * 1_048_576)
    with pytest.raises(ValueError, match="too much text"):
        ModelAdapterRequest(
            ModelOperation.COMPLETE,
            (item,) * (MAXIMUM_REQUEST_TEXT_BYTES // 1_048_576 + 1),
            1,
        )
    with pytest.raises(ValueError, match="invalid Unicode"):
        ModelAdapterRequest(
            ModelOperation.COMPLETE,
            (ModelMessage(MessageRole.USER, "\ud800"),),
            1,
        )


@pytest.mark.parametrize(
    "wire_model",
    [
        DEEPSEEK_V4_FLASH_WIRE_MODEL,
        "xiaomi/mimo-v2.5",
        "ibm-granite/granite-4.1-8b",
    ],
)
def test_non_streaming_maps_selected_text_model_usage_and_safe_headers(
    wire_model: str,
) -> None:
    harness = _Harness(_request())
    with _server(_Reply(body=_completion())) as server:
        plan = _plan(_endpoint(server), wire_model=wire_model)
        adapter = _adapter(harness)
        result = adapter.execute(plan, harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert {item.unit: item.quantity for item in result.usage} == {
        UsageUnit.REQUEST: Decimal(1),
        UsageUnit.INPUT_TOKEN: Decimal(10),
        UsageUnit.OUTPUT_TOKEN: Decimal(3),
        UsageUnit.CACHED_TOKEN: Decimal(2),
    }
    assert harness.events == [
        ModelOutputEvent(ModelOutputEventKind.DELTA, "test response"),
        ModelOutputEvent(ModelOutputEventKind.COMPLETED),
    ]
    captured = server.captured[0]
    body = json.loads(captured.body)
    assert captured.path == "/api/v1/chat/completions"
    assert body["model"] == wire_model
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert body["messages"] == [
        {"role": "user", "content": "Reply with a short test value."}
    ]
    assert captured.headers["authorization"] == f"Bearer {_TEST_SECRET.decode()}"
    assert all(lease.closed for lease in harness.leases)
    assert plan.typed_prices[0].price == Decimal("0.01")


def test_zero_cached_token_detail_does_not_create_unpriced_usage() -> None:
    harness = _Harness(_request())
    completion = json.loads(_completion())
    completion["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    with _server(
        _Reply(body=json.dumps(completion, separators=(",", ":")).encode())
    ) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert {item.unit: item.quantity for item in result.usage} == {
        UsageUnit.REQUEST: Decimal(1),
        UsageUnit.INPUT_TOKEN: Decimal(12),
        UsageUnit.OUTPUT_TOKEN: Decimal(3),
    }


def test_stream_maps_text_final_usage_and_done() -> None:
    chunks = (
        b'data: {"choices":[{"delta":{"content":"one "},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"two"},"finish_reason":"stop"}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert json.loads(server.captured[0].body)["stream"] is True
    assert [event.text for event in harness.events[:-1]] == ["one ", "two"]
    assert harness.events[-1].kind is ModelOutputEventKind.COMPLETED
    assert {item.unit: item.quantity for item in result.usage} == {
        UsageUnit.REQUEST: Decimal(1),
        UsageUnit.INPUT_TOKEN: Decimal(4),
        UsageUnit.OUTPUT_TOKEN: Decimal(2),
    }


def test_stream_accepts_official_reasoning_then_text_chunks() -> None:
    """Ignore optional reasoning data and accept the later public text delta."""
    chunks = (
        (
            b'data: {"id":"gen-1","object":"chat.completion.chunk",'
            b'"created":1,"model":"example/model","choices":[{"index":0,'
            b'"delta":{"role":"assistant","content":null,"reasoning":"think"},'
            b'"finish_reason":null}]}\n\n'
        ),
        (
            b'data: {"id":"gen-1","object":"chat.completion.chunk",'
            b'"created":1,"model":"example/model","choices":[{"index":0,'
            b'"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
        ),
        (
            b'data: {"id":"gen-1","object":"chat.completion.chunk",'
            b'"created":1,"model":"example/model","choices":[],"usage":'
            b'{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
        ),
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert [event.text for event in harness.events[:-1]] == ["answer"]
    assert harness.events[-1].kind is ModelOutputEventKind.COMPLETED


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_stream_accepts_repeated_identical_empty_terminal_metadata(
    finish_reason: str,
) -> None:
    first = json.dumps(
        {
            "choices": [
                {"delta": {"content": "answer"}, "finish_reason": finish_reason}
            ]
        },
        separators=(",", ":"),
    ).encode()
    repeated = json.dumps(
        {
            "choices": [
                {
                    "delta": {"content": None, "refusal": None},
                    "finish_reason": finish_reason,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    chunks = (
        b"data: " + first + b"\n\n",
        b"data: " + repeated + b"\n\n",
        (
            b'data: {"choices":[],"usage":{"prompt_tokens":1,'
            b'"completion_tokens":1}}\n\n'
        ),
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert [event.text for event in harness.events[:-1]] == ["answer"]


@pytest.mark.parametrize(
    ("delta", "finish_reason", "detail_code"),
    [
        ({"content": None}, "length", "stream_finish_conflict"),
        ({"content": "more"}, "stop", "stream_content_after_finish"),
        (
            {"content": None, "refusal": {"private": "value"}},
            "stop",
            "stream_refusal_type",
        ),
    ],
)
def test_stream_rejects_unsafe_repeated_terminal_metadata(
    delta: dict[str, object], finish_reason: str, detail_code: str
) -> None:
    repeated = json.dumps(
        {"choices": [{"delta": delta, "finish_reason": finish_reason}]},
        separators=(",", ":"),
    ).encode()
    chunks = (
        (
            b'data: {"choices":[{"delta":{"content":"answer"},'
            b'"finish_reason":"stop"}]}\n\n'
        ),
        b"data: " + repeated + b"\n\n",
        (
            b'data: {"choices":[],"usage":{"prompt_tokens":1,'
            b'"completion_tokens":1}}\n\n'
        ),
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.INTERRUPTED
    assert result.failure is not None
    assert result.failure.evidence.detail_code == detail_code


def test_stream_keeps_policy_semantics_for_repeated_terminal_refusal() -> None:
    chunks = (
        (
            b'data: {"choices":[{"delta":{"content":"answer"},'
            b'"finish_reason":"stop"}]}\n\n'
        ),
        (
            b'data: {"choices":[{"delta":{"content":null,'
            b'"refusal":"private provider refusal"},"finish_reason":"stop"}]}\n\n'
        ),
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.INTERRUPTED
    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.POLICY
    assert result.failure.evidence.detail_code == "provider_policy_refusal"
    assert "private provider refusal" not in repr(result)


@pytest.mark.parametrize(
    ("chunks", "detail_code", "outcome"),
    [
        (
            (
                b'data: {"choices":[{"delta":{"content":"text"},'
                b'"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1}}\n\n'
            ),
            "stream_missing_done",
            AttemptOutcome.INTERRUPTED,
        ),
        (
            (
                b'data: {"choices":[{"delta":{"content":"text"},'
                b'"finish_reason":null}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            ),
            "stream_missing_finish",
            AttemptOutcome.INTERRUPTED,
        ),
        (
            (
                b'data: {"choices":[{"delta":{"content":null,'
                b'"reasoning":"think"},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            ),
            "stream_missing_content",
            AttemptOutcome.FAILED,
        ),
        (
            (
                b'data: {"choices":[{"delta":{"content":"text"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
            "stream_missing_usage",
            AttemptOutcome.INTERRUPTED,
        ),
    ],
)
def test_stream_reports_the_closed_missing_terminal_condition(
    chunks: bytes, detail_code: str, outcome: AttemptOutcome
) -> None:
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(
        _Reply(content_type="text/event-stream", chunks=(chunks,))
    ) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is outcome
    assert result.failure is not None
    assert result.failure.evidence.detail_code == detail_code


@pytest.mark.parametrize(
    ("reply", "expected_class"),
    [
        (_Reply(body=b"{}"), TerminalErrorClass.INVALID_PROVIDER_RESPONSE),
        (
            _Reply(body=_completion(usage=False)),
            TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
        ),
        (
            _Reply(body=_completion(), content_type="text/plain"),
            TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
        ),
        (
            _Reply(
                body=b'{"choices":[{"message":{"content":"\\ud800"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            ),
            TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
        ),
    ],
)
def test_completion_rejects_malformed_body_content_type_usage_and_unicode(
    reply: _Reply, expected_class: TerminalErrorClass
) -> None:
    harness = _Harness(_request())
    with _server(reply) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.error.error_class is expected_class
    assert result.usage[0].unit is UsageUnit.REQUEST


def test_provider_error_uses_only_status_and_bounded_retry_evidence() -> None:
    private_error = b'{"error":{"message":"test-openrouter-key-placeholder"}}'
    harness = _Harness(_request())
    reply = _Reply(
        body=private_error,
        status=429,
        headers={"Content-Length": str(8_388_609), "Retry-After": "3"},
    )
    with _server(reply) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.RATE_LIMIT
    assert result.failure.error.affected_scope is ErrorScope.PROVIDER_INSTANCE
    assert result.failure.evidence.provider_status == 429
    assert result.failure.evidence.retry_after_ms == 3_000
    assert _TEST_SECRET.decode() not in repr(result)
    assert _TEST_SECRET.decode() not in str(result.failure.error)


@pytest.mark.parametrize(
    ("status", "error_class", "scope"),
    [
        (401, TerminalErrorClass.AUTHENTICATION, ErrorScope.CREDENTIAL),
        (403, TerminalErrorClass.POLICY, ErrorScope.PROVIDER_INSTANCE),
        (
            400,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
        ),
        (
            413,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
        ),
        (
            422,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
        ),
        (
            404,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
        ),
        (402, TerminalErrorClass.BUDGET, ErrorScope.PROVIDER_INSTANCE),
        (408, TerminalErrorClass.TIMEOUT, ErrorScope.ATTEMPT),
        (500, TerminalErrorClass.PROVIDER_UNAVAILABLE, ErrorScope.ATTEMPT),
    ],
)
def test_http_status_uses_the_safe_failure_class_and_scope(
    status: int, error_class: TerminalErrorClass, scope: ErrorScope
) -> None:
    harness = _Harness(_request())
    with _server(_Reply(status=status, body=b"unsafe provider detail")) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.failure is not None
    assert result.failure.error.error_class is error_class
    assert result.failure.error.affected_scope is scope
    assert result.failure.evidence.provider_status == status


def test_refusal_is_policy_and_inconsistent_cached_usage_is_invalid() -> None:
    refusal = json.dumps(
        {
            "choices": [
                {
                    "message": {"content": None, "refusal": "unsafe detail"},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode()
    bad_usage = json.dumps(
        {
            "choices": [{"message": {"content": "text"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }
    ).encode()
    results = []
    for body in (refusal, bad_usage):
        harness = _Harness(_request())
        with _server(_Reply(body=body)) as server:
            adapter = _adapter(harness)
            results.append(adapter.execute(_plan(_endpoint(server)), harness.progress))
            adapter.close()

    refusal_result, usage_result = results
    assert refusal_result.failure is not None
    assert refusal_result.failure.error.error_class is TerminalErrorClass.POLICY
    assert refusal_result.usage[0].unit is UsageUnit.REQUEST
    assert "unsafe detail" not in repr(refusal_result)
    assert usage_result.failure is not None
    assert (
        usage_result.failure.error.error_class
        is TerminalErrorClass.INVALID_PROVIDER_RESPONSE
    )


def test_completion_accepts_large_text_and_requires_terminal_finish() -> None:
    large_text = "x" * 1_100_000
    harness = _Harness(_request())
    with _server(_Reply(body=_completion(content=large_text))) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert "".join(event.text or "" for event in harness.events) == large_text

    invalid_body = json.dumps(
        {
            "choices": [{"message": {"content": "not terminal"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ).encode()
    invalid_harness = _Harness(_request())
    with _server(_Reply(body=invalid_body)) as server:
        invalid_adapter = _adapter(invalid_harness)
        invalid_result = invalid_adapter.execute(
            _plan(_endpoint(server)), invalid_harness.progress
        )
        invalid_adapter.close()

    assert invalid_result.outcome is AttemptOutcome.FAILED
    assert invalid_result.failure is not None
    assert (
        invalid_result.failure.error.error_class
        is TerminalErrorClass.INVALID_PROVIDER_RESPONSE
    )


def test_timeout_is_safe_and_keeps_request_usage() -> None:
    harness = _Harness(_request())
    reply = _Reply(body=_completion(), header_delay=0.25)
    with _server(reply) as server:
        plan = _plan(
            _endpoint(server),
            timeouts=AttemptTimeouts(100, 100, 100, 1_000),
        )
        adapter = _adapter(harness)
        result = adapter.execute(plan, harness.progress)
        adapter.close()

    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.TIMEOUT
    assert len(result.usage) == 1
    assert result.usage[0].unit is UsageUnit.REQUEST


def test_connected_progress_precedes_slow_response_headers() -> None:
    harness = _Harness(_request())
    reply = _Reply(body=_completion(), header_delay=0.2)
    connected = threading.Event()

    def progress(phase: AdapterPhase) -> None:
        harness.progress(phase)
        if phase is AdapterPhase.CONNECTED:
            connected.set()

    with _server(reply) as server:
        adapter = _adapter(harness)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                adapter.execute, _plan(_endpoint(server)), progress
            )
            assert connected.wait(timeout=0.05)
            assert not future.done()
            assert future.result(timeout=2).outcome is AttemptOutcome.SUCCEEDED
        adapter.close()


def test_compressed_response_is_decoded_then_bounded() -> None:
    harness = _Harness(_request())
    reply = _Reply(
        body=gzip.compress(_completion()),
        headers={"Content-Encoding": "gzip"},
    )
    with _server(reply) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.SUCCEEDED


def test_response_declared_body_size_is_bounded_before_read() -> None:
    harness = _Harness(_request())
    reply = _Reply(
        body=b"unsafe provider detail",
        headers={"Content-Length": str(8_388_609)},
    )
    with _server(reply) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.error.error_class is (
        TerminalErrorClass.INVALID_PROVIDER_RESPONSE
    )


def test_redirect_is_not_followed() -> None:
    harness = _Harness(_request())
    reply = _Reply(status=302, headers={"Location": "/credential-target"})
    with _server(reply) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert len(server.captured) == 1


def test_unsupported_capability_fails_before_credential_or_network() -> None:
    harness = _Harness(_request())
    with _server(_Reply(body=_completion())) as server:
        adapter = _adapter(harness)
        result = adapter.execute(
            _plan(_endpoint(server), capabilities=frozenset({"embedding.batch"})),
            harness.progress,
        )
        adapter.close()

    assert result.failure is not None
    assert result.failure.evidence.detail_code == "unsupported_capability"
    assert harness.credential_calls == 0
    assert server.captured == []


def test_invalid_attribution_fails_before_credential_or_network() -> None:
    harness = _Harness(_request())
    with _server(_Reply(body=_completion())) as server:
        plan = _plan(_endpoint(server))
        adapter = _adapter(harness)
        result = adapter.execute(
            replace(
                plan,
                instance_settings={
                    **plan.instance_settings,
                    "attribution_referer": "https://example.test/privé",
                },
            ),
            harness.progress,
        )
        adapter.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "invalid_attribution_settings"
    assert harness.credential_calls == 0
    assert server.captured == []


def test_closed_adapter_rejects_new_work_before_credential_or_network() -> None:
    harness = _Harness(_request())
    with _server(_Reply(body=_completion())) as server:
        adapter = _adapter(harness)
        adapter.close()
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "adapter_closed"
    assert harness.credential_calls == 0
    assert server.captured == []


def test_cancellation_during_preflight_does_not_start_provider_work() -> None:
    request_started = threading.Event()
    release_request = threading.Event()
    harness = _Harness(_request())

    def blocked_request_source(_plan: AttemptPlan) -> ModelAdapterRequest:
        request_started.set()
        assert release_request.wait(timeout=2)
        return harness.request

    with _server(_Reply(body=_completion())) as server:
        plan = _plan(_endpoint(server))
        adapter = OpenRouterAdapter(
            requests=blocked_request_source,
            credentials=harness.credential_source,
            output=harness.output,
            allow_loopback_test_endpoint=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(adapter.execute, plan, harness.progress)
            assert request_started.wait(timeout=1)
            evidence = adapter.cancel(plan)
            release_request.set()
            result = future.result(timeout=2)
        adapter.close()

    assert evidence.stop_requested
    assert evidence.confirmed_stopped
    assert result.outcome is AttemptOutcome.UNCERTAIN
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "local_stop_before_submit"
    assert harness.credential_calls == 0
    assert server.captured == []


def test_cancellation_closes_only_the_attempt_transport_without_false_proof() -> None:
    release = threading.Event()
    chunks = (
        b'data: {"choices":[{"delta":{"content":"visible"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    reply = _Reply(
        content_type="text/event-stream",
        chunks=chunks,
        pause_after_first=release,
    )
    with _server(reply) as server:
        plan = _plan(_endpoint(server))
        adapter = _adapter(harness)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(adapter.execute, plan, harness.progress)
            assert server.first_chunk_sent.wait(timeout=1)
            evidence = adapter.cancel(plan)
            release.set()
            result = future.result(timeout=2)
        adapter.close()

    assert evidence.supported
    assert evidence.stop_requested
    assert not evidence.confirmed_stopped
    assert result.outcome is AttemptOutcome.UNCERTAIN
    assert all(lease.closed for lease in harness.leases)


def test_stream_rejects_oversized_delta_and_requires_done_and_final_usage() -> None:
    oversized = "x" * 262_145
    oversized_event = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": oversized}, "finish_reason": None}]}
        )
        + "\n\n"
    ).encode()
    cases = (
        (oversized_event, AttemptOutcome.FAILED),
        (
            (
                b'data: {"choices":[{"delta":{"content":"partial"},'
                b'"finish_reason":"stop"}]}\n\n'
            ),
            AttemptOutcome.INTERRUPTED,
        ),
        (
            (
                b'data: {"choices":[{"delta":{"content":"text"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
            AttemptOutcome.INTERRUPTED,
        ),
        (
            (
                b'data: {"choices":[{"delta":{"content":"text"},'
                b'"finish_reason":null}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            ),
            AttemptOutcome.INTERRUPTED,
        ),
    )
    for payload, outcome in cases:
        harness = _Harness(_request(ModelOperation.STREAM))
        with _server(
            _Reply(content_type="text/event-stream", chunks=(payload,))
        ) as server:
            adapter = _adapter(harness)
            result = adapter.execute(_plan(_endpoint(server)), harness.progress)
            adapter.close()
        assert result.outcome is outcome


@pytest.mark.parametrize(
    ("prefix", "outcome"),
    [
        (b"", AttemptOutcome.FAILED),
        (
            (
                b'data: {"choices":[{"delta":{"content":"partial"},'
                b'"finish_reason":null}]}\n\n'
            ),
            AttemptOutcome.INTERRUPTED,
        ),
    ],
)
def test_stream_uses_safe_typed_provider_error(
    prefix: bytes, outcome: AttemptOutcome
) -> None:
    error = (
        b'data: {"error":{"code":429,"message":"unsafe detail",'
        b'"metadata":{"error_type":"rate_limit_exceeded"}},'
        b'"choices":[{"delta":{"content":""},"finish_reason":"error"}]}\n\n'
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(
        _Reply(content_type="text/event-stream", chunks=(prefix + error,))
    ) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is outcome
    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.RATE_LIMIT
    assert result.failure.error.affected_scope is ErrorScope.PROVIDER_INSTANCE
    assert result.failure.evidence.provider_status == 429
    assert "unsafe detail" not in repr(result)


def test_stream_rejects_conflicting_usage_reports() -> None:
    chunks = (
        b'data: {"choices":[{"delta":{"content":"text"},"finish_reason":"stop"}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
        b"data: [DONE]\n\n",
    )
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(_Reply(content_type="text/event-stream", chunks=chunks)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.INTERRUPTED
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "stream_usage_conflict"


def test_stream_bounds_the_aggregate_output_event_count() -> None:
    event = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
    harness = _Harness(_request(ModelOperation.STREAM))
    with _server(
        _Reply(content_type="text/event-stream", chunks=(event * 10_001,))
    ) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.INTERRUPTED
    assert len(harness.events) == 10_000
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "stream_output_events_limit"


def test_stream_invalid_response_branches_have_fixed_safe_codes() -> None:
    """Keep every stream parser rejection classified without provider data."""
    parser_functions = {
        "_read_stream",
        "_sse_data",
        "_json_object",
        "_unique_json_object",
        "_invalid_json_constant",
        "_strict_json_float",
        "_usage",
        "_stream_provider_failure",
        "_validate_response_headers",
        "_validate_content_type",
    }
    expected = {
        "response_content_length",
        "response_content_type",
        "response_header_limits",
        "response_json_depth",
        "response_json_duplicate_field",
        "response_json_encoding",
        "response_json_non_finite",
        "response_json_object",
        "response_json_size",
        "response_json_syntax",
        "response_json_value",
        "response_redirect_history",
        "response_usage_cached_tokens",
        "response_usage_details",
        "response_usage_inconsistent",
        "response_usage_object",
        "response_usage_tokens",
        "stream_body_limit",
        "stream_buffer_limit",
        "stream_choice_object",
        "stream_choices_shape",
        "stream_content_after_finish",
        "stream_content_encoding",
        "stream_content_type",
        "stream_data_after_done",
        "stream_delta_limit",
        "stream_delta_object",
        "stream_error_object",
        "stream_error_status",
        "stream_error_type",
        "stream_event_limit",
        "stream_finish_conflict",
        "stream_finish_type",
        "stream_finish_value",
        "stream_output_bytes_limit",
        "stream_output_events_limit",
        "stream_refusal_type",
        "stream_sse_field",
        "stream_sse_tail",
        "stream_usage_conflict",
    }
    tree = ast.parse(inspect.getsource(openrouter_module))
    found: set[str] = set()
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in parser_functions
    ):
        for node in ast.walk(function):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            call = node.exc
            if (
                not isinstance(call.func, ast.Name)
                or call.func.id != "_InvalidResponseError"
            ):
                continue
            assert len(call.args) == 1
            argument = call.args[0]
            assert isinstance(argument, ast.Constant)
            assert isinstance(argument.value, str)
            found.add(argument.value)

    assert found == expected


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[],"usage":NaN}',
        b'{"choices":[],"usage":{},"unknown":1e999}',
        b'{"choices":[],"choices":[],"usage":{}}',
        b'{"nested":' + (b"[" * 2_000) + b"0" + (b"]" * 2_000) + b"}",
    ],
)
def test_completion_rejects_non_standard_or_ambiguous_json(body: bytes) -> None:
    harness = _Harness(_request())
    with _server(_Reply(body=body)) as server:
        adapter = _adapter(harness)
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.error.error_class is (
        TerminalErrorClass.INVALID_PROVIDER_RESPONSE
    )


def test_sink_failure_returns_safe_uncertain_result_with_usage() -> None:
    harness = _Harness(_request())

    def failed_sink(_plan: AttemptPlan, _event: ModelOutputEvent) -> None:
        raise RuntimeError(_TEST_SECRET.decode())

    with _server(_Reply(body=_completion())) as server:
        adapter = OpenRouterAdapter(
            requests=harness.request_source,
            credentials=harness.credential_source,
            output=failed_sink,
            allow_loopback_test_endpoint=True,
        )
        result = adapter.execute(_plan(_endpoint(server)), harness.progress)
        adapter.close()

    assert result.outcome is AttemptOutcome.UNCERTAIN
    assert result.failure is not None
    assert result.failure.evidence.detail_code == "output_sink_failed"
    assert _TEST_SECRET.decode() not in repr(result)
    assert {item.unit for item in result.usage} >= {
        UsageUnit.REQUEST,
        UsageUnit.INPUT_TOKEN,
        UsageUnit.OUTPUT_TOKEN,
    }


def test_loopback_override_rejects_host_alias_and_wire_model_stays_configurable() -> (
    None
):
    harness = _Harness(_request())
    adapter = _adapter(harness)
    alias_result = adapter.execute(
        _plan("http://localhost:9999/api/v1"), harness.progress
    )
    configured_result = adapter.execute(
        replace(
            _plan("http://localhost:9999/api/v1"),
            wire_model="another-provider/configured-model",
        ),
        harness.progress,
    )
    adapter.close()

    assert alias_result.failure is not None
    assert alias_result.failure.evidence.detail_code == "endpoint_not_allowed"
    assert configured_result.failure is not None
    assert configured_result.failure.evidence.detail_code == "endpoint_not_allowed"
    assert harness.credential_calls == 0

    configured_harness = _Harness(_request())
    with _server(_Reply(body=_completion())) as server:
        configured_adapter = _adapter(configured_harness)
        success = configured_adapter.execute(
            _plan(
                _endpoint(server),
                wire_model="another-provider/configured-model",
            ),
            configured_harness.progress,
        )
        configured_adapter.close()
    assert success.outcome is AttemptOutcome.SUCCEEDED
    assert json.loads(server.captured[0].body)["model"] == (
        "another-provider/configured-model"
    )

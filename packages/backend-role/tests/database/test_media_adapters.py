"""WaveSpeed media adapter contract tests with a deterministic HTTP transport."""
# ruff: noqa: D103, PLR2004

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx
import pytest
from llmrouter_backend.adapters import WaveSpeedMediaAdapter
from llmrouter_backend.calls import (
    CallRequirements,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOutput,
)
from llmrouter_backend.catalog import ProviderRoute
from llmrouter_backend.diagnostics import CapturedMedia
from llmrouter_backend.models import ModelConstraints
from opendle import CallFailurePhase

from .provider_adapter_conformance import (
    FailureCase,
    SuccessCase,
    assert_failure,
    assert_success,
    capture_attempt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_PRICED_UNITS = frozenset({"image", "video_second", "audio_second"})


class OversizedJsonStream(httpx.AsyncByteStream):
    """Yield more than the accepted JSON bound without a length header."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield bounded chunks whose total is too large."""
        for _ in range(17):
            yield b"x" * (64 * 1024)


class OneChunkStream(httpx.AsyncByteStream):
    """Yield one response body without eager HTTPX decoding."""

    def __init__(self, body: bytes) -> None:
        """Keep the exact streamed body."""
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the configured body once."""
        yield self.body


def _request(kind: str = "image", *, image: bool = False) -> ProviderAttemptRequest:
    media = (CapturedMedia(b"input-image", "image/png", "input"),) if image else ()
    return ProviderAttemptRequest(
        route=ProviderRoute(
            "wavespeed-model",
            "wavespeed-provider",
            "wavespeed",
            None,
            "wavespeed-ai/example-model",
            "wavespeed-key",
            ModelConstraints(),
            None,
            None,
        ),
        request_json=json.dumps(
            {
                "workspace_api_name": "main",
                "selector": {"provider_model_api_name": "wavespeed-model"},
                "kind": kind,
                "prompt": "Create retained media.",
            },
            separators=(",", ":"),
        ),
        credential="private-control-value",
        kind="media",
        requirements=CallRequirements(
            frozenset({"text", "image"} if image else {"text"}),
            kind,
            input_image_sizes=tuple(len(item.body) for item in media),
        ),
        streaming=False,
        expected_embedding_count=None,
        input_media=media,
    )


@pytest.mark.parametrize(
    ("kind", "content_type", "body", "unit"),
    [
        ("image", "image/png", b"\x89PNG\r\n\x1a\npng-result", "image"),
        ("video", "video/mp4", b"\x00\x00\x00\x18ftypisomvideo", "video_second"),
        ("audio", "audio/mpeg", b"ID3audio-result", "audio_second"),
    ],
)
def test_wavespeed_submits_polls_and_downloads_one_bounded_result(
    kind: str, content_type: str, body: bytes, unit: str
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            submitted = json.loads(request.content)
            assert submitted["prompt"] == "Create retained media."
            assert request.headers["authorization"] == "Bearer private-control-value"
            return httpx.Response(
                200,
                json={"code": 200, "data": {"id": "task-1", "status": "pending"}},
                headers={"Content-Type": "application/json"},
            )
        if request.url.host == "api.wavespeed.ai":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "id": "task-1",
                        "status": "completed",
                        "outputs": [f"https://storage.wavespeed.ai/result.{kind}"],
                        "duration": 2.5,
                    },
                },
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(handler), poll_interval_seconds=0
    )
    request = _request(kind)
    captured = asyncio.run(capture_attempt(adapter, request))
    case = SuccessCase(kind, ("media",), frozenset({unit}))

    assert_success(adapter, request, captured, case, priced_usage_units=_PRICED_UNITS)
    assert len(calls) == 3
    output = captured.events[0]
    assert isinstance(output, ProviderOutput)
    assert output.media_body == body
    completion = captured.events[1]
    assert isinstance(completion, ProviderCompleted)
    assert {item.unit for item in completion.usage} == {unit}
    assert all(call.headers["accept-encoding"] == "identity" for call in calls)


def test_wavespeed_encodes_input_images_without_exposing_control_values() -> None:
    submitted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "completed",
                        "outputs": ["https://cdn.wavespeed.ai/result.png"],
                    }
                },
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nresult",
            headers={"Content-Type": "image/png"},
        )

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(handler), poll_interval_seconds=0
    )
    captured = asyncio.run(capture_attempt(adapter, _request(image=True)))

    assert captured.failure is None
    assert submitted["images"] == ["data:image/png;base64,aW5wdXQtaW1hZ2U="]
    assert "private-control-value" not in json.dumps(submitted)


@pytest.mark.parametrize(
    ("status", "failure_class"),
    [
        (401, "authentication"),
        (429, "rate_limited"),
        (422, "incompatible"),
        (503, "unavailable"),
    ],
)
def test_wavespeed_confirmed_submit_rejection_allows_fallback(
    status: int, failure_class: str
) -> None:
    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(lambda _request: httpx.Response(status)),
        poll_interval_seconds=0,
    )
    request = _request()
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case(failure_class, CallFailurePhase.BEFORE_VISIBLE_OUTPUT),
        priced_usage_units=_PRICED_UNITS,
    )


def test_wavespeed_rejects_redirects_and_untrusted_result_hosts_after_submit() -> None:
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": {"id": "task", "status": "pending"}},
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(redirect_handler), poll_interval_seconds=0
    )
    request = _request()
    redirected = asyncio.run(capture_attempt(adapter, request))
    assert_failure(
        adapter,
        request,
        redirected,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )

    requests = 0

    def hostile_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "completed",
                    "outputs": ["https://127.0.0.1/private"],
                }
            },
            headers={"Content-Type": "application/json"},
        )

    hostile = asyncio.run(
        capture_attempt(
            WaveSpeedMediaAdapter(
                httpx.MockTransport(hostile_handler), poll_interval_seconds=0
            ),
            request,
        )
    )
    assert hostile.failure is not None
    assert hostile.failure.failure_class == "invalid_response"
    assert hostile.failure.phase is CallFailurePhase.UNCERTAIN
    assert requests == 1


def test_wavespeed_rejects_video_without_provider_duration() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.wavespeed.ai":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "completed",
                        "outputs": ["https://storage.wavespeed.ai/result.mp4"],
                    }
                },
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            content=b"\x00\x00\x00\x18ftypisomvideo",
            headers={"Content-Type": "video/mp4"},
        )

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(handler), poll_interval_seconds=0
    )
    request = _request("video")
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )


@pytest.mark.parametrize(
    "unsafe_headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Length": str(1024 * 1024 + 1)},
        {"Content-Length": "invalid"},
        {"Content-Length": "1"},
    ],
)
def test_wavespeed_rejects_unsafe_json_headers(
    unsafe_headers: dict[str, str],
) -> None:
    headers = {"Content-Type": "application/json", **unsafe_headers}
    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                stream=OneChunkStream(b'{"data":{"status":"completed","outputs":[]}}'),
                headers=headers,
            )
        ),
        poll_interval_seconds=0,
    )
    request = _request()
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )


def test_wavespeed_rejects_oversized_streamed_json() -> None:
    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                stream=OversizedJsonStream(),
                headers={"Content-Type": "application/json"},
            )
        ),
        poll_interval_seconds=0,
    )
    request = _request()
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )


@pytest.mark.parametrize(
    "unsafe_headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Length": str(1024 * 1024 * 1024 + 1)},
        {"Content-Length": "invalid"},
        {"Content-Length": "1"},
    ],
)
def test_wavespeed_rejects_unsafe_media_headers(
    unsafe_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.wavespeed.ai":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "completed",
                        "outputs": ["https://storage.wavespeed.ai/result.png"],
                    }
                },
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            stream=OneChunkStream(b"\x89PNG\r\n\x1a\nresult"),
            headers={"Content-Type": "image/png", **unsafe_headers},
        )

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(handler), poll_interval_seconds=0
    )
    request = _request()
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )


@pytest.mark.parametrize("response_kind", ["json", "media", "error"])
@pytest.mark.parametrize("header_failure", ["duplicate-type", "count", "bytes"])
def test_wavespeed_rejects_duplicate_or_unbounded_response_headers(
    response_kind: str,
    header_failure: str,
) -> None:
    content_type = "image/png" if response_kind == "media" else "application/json"
    headers: list[tuple[str, str]] = [("Content-Type", content_type)]
    if header_failure == "duplicate-type":
        headers.append(("Content-Type", content_type))
    elif header_failure == "count":
        headers.extend((f"X-Test-{index}", "x") for index in range(128))
    else:
        headers.append(("X-Oversized", "x" * (64 * 1024)))

    def handler(request: httpx.Request) -> httpx.Response:
        if response_kind == "error":
            return httpx.Response(503, stream=OneChunkStream(b"error"), headers=headers)
        if response_kind == "json" or request.url.host != "api.wavespeed.ai":
            body = (
                b'{"data":{"status":"completed","outputs":[]}}'
                if response_kind == "json"
                else b"\x89PNG\r\n\x1a\nresult"
            )
            return httpx.Response(200, stream=OneChunkStream(body), headers=headers)
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "completed",
                    "outputs": ["https://storage.wavespeed.ai/result.png"],
                }
            },
            headers={"Content-Type": "application/json"},
        )

    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(handler), poll_interval_seconds=0
    )
    request = _request()
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("invalid_response", CallFailurePhase.UNCERTAIN),
        priced_usage_units=_PRICED_UNITS,
    )


def test_wavespeed_rejects_an_oversized_bearer_control() -> None:
    adapter = WaveSpeedMediaAdapter(
        httpx.MockTransport(lambda _request: pytest.fail("No request is allowed.")),
        poll_interval_seconds=0,
    )
    request = replace(_request(), credential="x" * 10_001)
    captured = asyncio.run(capture_attempt(adapter, request))

    assert_failure(
        adapter,
        request,
        captured,
        _failure_case("incompatible", CallFailurePhase.BEFORE_VISIBLE_OUTPUT),
        priced_usage_units=_PRICED_UNITS,
    )


def _failure_case(failure_class: str, phase: CallFailurePhase) -> FailureCase:
    return FailureCase(failure_class, visible_before_failure=False, phase=phase)

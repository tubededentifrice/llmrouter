"""Native FastAPI model-request routes."""
# ruff: noqa: EM101, PLR2004, TRY003

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llmrouter_backend.execution import KEEPALIVE_SECONDS, keepalive

from .model import (
    MAXIMUM_AUTHORIZATION_CHARACTERS,
    MAXIMUM_HTTP_BODY_BYTES,
    MAXIMUM_LAST_EVENT_ID_CHARACTERS,
    ModelRequestError,
)
from .service import ModelRequestService

router = APIRouter(prefix="/v1", tags=["Requests"])

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from llmrouter_backend.execution import StreamEvent

    from .model import ScopedRequest


def install_model_request_service(app: FastAPI, service: ModelRequestService) -> None:
    """Install one explicit service on a FastAPI-compatible application."""
    state = getattr(app, "state", None)
    if state is None:
        raise TypeError("The application does not have state storage.")
    state.model_request_service = service


@router.post("/model-requests", response_model=None)
async def create_model_request(request: Request) -> Response:
    """Create or replay one authenticated native request."""
    error_request_id = _error_request_id()
    try:
        service = _service(request, error_request_id)
        token = _bearer_token(request, error_request_id)
        logical_request_id = _single_header(
            request,
            "x-llmrouter-request-id",
            maximum=36,
            error_request_id=error_request_id,
        )
        _require_json_content_type(request, error_request_id)
        body = await _bounded_body(request, error_request_id)
        result = await asyncio.to_thread(
            service.create,
            token,
            logical_request_id,
            body,
            error_request_id=error_request_id,
        )
        return JSONResponse(
            result.receipt,
            status_code=result.status_code,
            headers={"Cache-Control": "no-store"},
        )
    except ModelRequestError as error:
        return _error_response(error)


@router.post("/chat/completions", response_model=None)
async def create_compatible_chat_completion(request: Request) -> Response:
    """Run one accepted OpenAI-compatible chat completion."""
    error_request_id = _error_request_id()
    try:
        service = _service(request, error_request_id)
        token = _bearer_token(request, error_request_id)
        logical_request_id = _single_header(
            request,
            "x-llmrouter-request-id",
            maximum=36,
            error_request_id=error_request_id,
        )
        _require_json_content_type(request, error_request_id)
        body = await _bounded_body(request, error_request_id)
        result, stream = await asyncio.to_thread(
            service.create_compatible_chat,
            token,
            logical_request_id,
            body,
            error_request_id=error_request_id,
        )
        scoped = await asyncio.to_thread(
            service.authorize_existing,
            token,
            logical_request_id,
            "model.read",
            error_request_id=error_request_id,
        )
        if stream:
            initial = await asyncio.to_thread(
                service.events,
                scoped,
                logical_request_id,
                after_sequence=0,
                error_request_id=error_request_id,
            )
            return StreamingResponse(
                _compatible_records(
                    request,
                    service,
                    scoped,
                    logical_request_id,
                    result.receipt,
                    initial,
                    error_request_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        status = await _wait_compatible_status(
            request,
            service,
            scoped,
            logical_request_id,
            error_request_id,
        )
        return JSONResponse(
            _compatible_response(status, result.receipt),
            headers={"Cache-Control": "no-store"},
        )
    except ModelRequestError as error:
        return _error_response(error)


@router.get("/model-requests/{request_id}", response_model=None)
async def get_model_request(request: Request, request_id: str) -> Response:
    """Read one bounded current or terminal status."""
    error_request_id = _error_request_id()
    try:
        service = _service(request, error_request_id)
        token = _bearer_token(request, error_request_id)
        scoped = await asyncio.to_thread(
            service.authorize_existing,
            token,
            request_id,
            "model.read",
            error_request_id=error_request_id,
        )
        status = await asyncio.to_thread(
            service.status,
            scoped,
            request_id,
            error_request_id=error_request_id,
        )
        return JSONResponse(status, headers={"Cache-Control": "no-store"})
    except ModelRequestError as error:
        return _error_response(error)


@router.post("/model-requests/{request_id}/cancel", response_model=None)
async def cancel_model_request(request: Request, request_id: str) -> Response:
    """Request idempotent best-effort cancellation."""
    error_request_id = _error_request_id()
    try:
        service = _service(request, error_request_id)
        token = _bearer_token(request, error_request_id)
        scoped = await asyncio.to_thread(
            service.authorize_existing,
            token,
            request_id,
            "model.cancel",
            error_request_id=error_request_id,
        )
        _require_json_content_type(request, error_request_id)
        body = await _bounded_body(request, error_request_id)
        status = await asyncio.to_thread(
            service.cancel,
            scoped,
            request_id,
            body,
            error_request_id=error_request_id,
        )
        return JSONResponse(status, headers={"Cache-Control": "no-store"})
    except ModelRequestError as error:
        return _error_response(error)


@router.get("/model-requests/{request_id}/events", response_model=None)
async def stream_model_request(request: Request, request_id: str) -> Response:
    """Stream retained and new native version-one SSE events."""
    error_request_id = _error_request_id()
    try:
        service = _service(request, error_request_id)
        token = _bearer_token(request, error_request_id)
        _require_stream_accept(request, error_request_id)
        after_sequence = _last_event_id(request, error_request_id)
        scoped = await asyncio.to_thread(
            service.authorize_existing,
            token,
            request_id,
            "model.read",
            error_request_id=error_request_id,
        )
        initial = await asyncio.to_thread(
            service.events,
            scoped,
            request_id,
            after_sequence=after_sequence,
            error_request_id=error_request_id,
        )
    except ModelRequestError as error:
        return _error_response(error)

    async def records() -> AsyncIterator[str]:
        cursor = after_sequence
        pending = initial
        last_write = time.monotonic()
        try:
            while True:
                for event in pending:
                    cursor = event.sequence
                    last_write = time.monotonic()
                    yield event.sse()
                    if event.event_name == "request.terminal":
                        return
                if await request.is_disconnected():
                    return
                status = await asyncio.to_thread(
                    service.execution_status,
                    scoped,
                    request_id,
                    error_request_id=error_request_id,
                )
                if status.terminal:
                    pending = await asyncio.to_thread(
                        service.events,
                        scoped,
                        request_id,
                        after_sequence=cursor,
                        error_request_id=error_request_id,
                    )
                    if pending:
                        continue
                    return
                elapsed = time.monotonic() - last_write
                if elapsed >= KEEPALIVE_SECONDS:
                    last_write = time.monotonic()
                    yield keepalive()
                await asyncio.sleep(0.1)
                pending = await asyncio.to_thread(
                    service.events,
                    scoped,
                    request_id,
                    after_sequence=cursor,
                    error_request_id=error_request_id,
                )
        finally:
            with suppress(ModelRequestError):
                await asyncio.to_thread(
                    service.disconnect,
                    scoped,
                    request_id,
                    error_request_id=error_request_id,
                )

    return StreamingResponse(
        records(),
        media_type="text/event-stream; llmrouter-stream=1",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _wait_compatible_status(
    request: Request,
    service: ModelRequestService,
    scoped: ScopedRequest,
    request_id: str,
    error_request_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status = await asyncio.to_thread(
            service.status,
            scoped,
            request_id,
            error_request_id=error_request_id,
        )
        if status.get("state") in {
            "succeeded",
            "failed",
            "interrupted",
            "cancelled",
            "uncertain",
        }:
            return status
        if await request.is_disconnected():
            raise ModelRequestError(
                "temporarily_unavailable",
                503,
                "The request continues. Read its native status before a retry.",
                error_request_id,
                retryable=True,
            )
        await asyncio.sleep(0.1)
    raise ModelRequestError(
        "temporarily_unavailable",
        503,
        "The request continues. Read its native status before a retry.",
        error_request_id,
        retryable=True,
    )


async def _compatible_records(  # noqa: PLR0913, PLR0917
    request: Request,
    service: ModelRequestService,
    scoped: ScopedRequest,
    request_id: str,
    receipt: dict[str, object],
    initial: tuple[StreamEvent, ...],
    error_request_id: str,
) -> AsyncIterator[str]:
    cursor = 0
    pending = initial
    last_write = time.monotonic()
    try:
        while True:
            for event in pending:
                cursor = event.sequence
                record = _compatible_event(event, receipt)
                if record is not None:
                    last_write = time.monotonic()
                    yield record
                if event.event_name == "request.terminal":
                    return
            if await request.is_disconnected():
                return
            status = await asyncio.to_thread(
                service.execution_status,
                scoped,
                request_id,
                error_request_id=error_request_id,
            )
            if status.terminal:
                pending = await asyncio.to_thread(
                    service.events,
                    scoped,
                    request_id,
                    after_sequence=cursor,
                    error_request_id=error_request_id,
                )
                if pending:
                    continue
                return
            if time.monotonic() - last_write >= KEEPALIVE_SECONDS:
                last_write = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(0.1)
            pending = await asyncio.to_thread(
                service.events,
                scoped,
                request_id,
                after_sequence=cursor,
                error_request_id=error_request_id,
            )
    finally:
        with suppress(ModelRequestError):
            await asyncio.to_thread(
                service.disconnect,
                scoped,
                request_id,
                error_request_id=error_request_id,
            )


def _compatible_event(
    event: StreamEvent, receipt: dict[str, object]
) -> str | None:
    if event.event_name == "output.delta":
        delta = event.payload.get("delta")
        if not isinstance(delta, str):
            return None
        return _compatible_sse(
            {
                "id": f"chatcmpl-{event.target.public_id}",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": None,
                    }
                ],
                "x_llmrouter_request_id": event.target.public_id,
                "x_llmrouter_state": "running",
                "x_llmrouter_status_url": receipt["status_url"],
            }
        )
    if event.event_name != "request.terminal":
        return None
    state = event.payload.get("state")
    if state == "succeeded":
        terminal = _compatible_sse(
            {
                "id": f"chatcmpl-{event.target.public_id}",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "x_llmrouter_request_id": event.target.public_id,
                "x_llmrouter_state": state,
                "x_llmrouter_status_url": receipt["status_url"],
            }
        )
        return terminal + "data: [DONE]\n\n"
    return _compatible_sse(
        {
            "error": {
                "code": "request_failed",
                "message": "The Router could not complete the request.",
                "type": "router_error",
            },
            "x_llmrouter_request_id": event.target.public_id,
            "x_llmrouter_state": state,
            "x_llmrouter_status_url": receipt["status_url"],
        }
    )


def _compatible_sse(document: dict[str, object]) -> str:
    return (
        "data: "
        + json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n\n"
    )


def _compatible_response(
    status: dict[str, object], receipt: dict[str, object]
) -> dict[str, object]:
    request_id = str(status["request_id"])
    document: dict[str, object] = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "model": status.get("assignment"),
        "x_llmrouter_request_id": request_id,
        "x_llmrouter_state": status["state"],
        "x_llmrouter_status_url": receipt["status_url"],
    }
    result = status.get("result")
    outputs = result.get("outputs") if isinstance(result, dict) else None
    output = outputs[0] if isinstance(outputs, list) and outputs else None
    text = output.get("text") if isinstance(output, dict) else None
    if isinstance(text, str) and status.get("state") == "succeeded":
        document["choices"] = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ]
    return document


def _service(request: Request, request_id: str) -> ModelRequestService:
    service = getattr(request.app.state, "model_request_service", None)
    if not isinstance(service, ModelRequestService):
        raise ModelRequestError(
            "temporarily_unavailable",
            503,
            "The Router is temporarily unavailable.",
            request_id,
            retryable=True,
        )
    return service


def _bearer_token(request: Request, request_id: str) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1 or len(values[0]) > MAXIMUM_AUTHORIZATION_CHARACTERS:
        raise _invalid_token(request_id)
    scheme, separator, token = values[0].partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not 43 <= len(token) <= 200
        or not token.isascii()
        or any(not (character.isalnum() or character in "_-") for character in token)
    ):
        raise _invalid_token(request_id)
    return token


def _single_header(
    request: Request,
    name: str,
    *,
    maximum: int,
    error_request_id: str,
) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > maximum:
        raise ModelRequestError(
            "invalid_request",
            400,
            "The request is invalid.",
            error_request_id,
        )
    return values[0]


def _require_json_content_type(request: Request, request_id: str) -> None:
    values = request.headers.getlist("content-type")
    if len(values) != 1 or len(values[0]) > 100:
        raise ModelRequestError(
            "invalid_request", 400, "The request must use JSON.", request_id
        )
    value = values[0]
    media_type, _, parameters = value.partition(";")
    if media_type.strip().lower() != "application/json" or (
        parameters and parameters.strip().lower() != "charset=utf-8"
    ):
        raise ModelRequestError(
            "invalid_request", 400, "The request must use JSON.", request_id
        )


async def _bounded_body(request: Request, request_id: str) -> bytes:
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        raise _body_too_large(request_id)
    content_length = content_lengths[0] if content_lengths else None
    if content_length is not None and (
        len(content_length) > 20
        or not content_length.isdecimal()
        or int(content_length) > MAXIMUM_HTTP_BODY_BYTES
    ):
        raise _body_too_large(request_id)
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAXIMUM_HTTP_BODY_BYTES - len(body):
            raise _body_too_large(request_id)
        body.extend(chunk)
    if not body:
        raise ModelRequestError(
            "invalid_request", 400, "The request body is required.", request_id
        )
    return bytes(body)


def _require_stream_accept(request: Request, request_id: str) -> None:
    values = request.headers.getlist("accept")
    if len(values) != 1 or len(values[0]) > 200:
        raise _unsupported_stream(request_id)
    parts = [part.strip() for part in values[0].split(";")]
    if parts[0].lower() != "text/event-stream" or "llmrouter-stream=1" not in {
        part.lower() for part in parts[1:]
    }:
        raise _unsupported_stream(request_id)


def _last_event_id(request: Request, request_id: str) -> int:
    values = request.headers.getlist("last-event-id")
    if not values:
        return 0
    value = _single_header(
        request,
        "last-event-id",
        maximum=MAXIMUM_LAST_EVENT_ID_CHARACTERS,
        error_request_id=request_id,
    )
    if not value.isdecimal():
        raise ModelRequestError(
            "invalid_request", 400, "The stream cursor is invalid.", request_id
        )
    sequence = int(value)
    if sequence > 9_223_372_036_854_775_807:
        raise ModelRequestError(
            "invalid_request", 400, "The stream cursor is invalid.", request_id
        )
    return sequence


def _error_response(error: ModelRequestError) -> JSONResponse:
    body: dict[str, object] = {
        "code": error.code,
        "message": str(error),
        "retryable": error.retryable,
        "request_id": error.request_id,
    }
    if error.retry_after_ms is not None:
        body["retry_after_ms"] = error.retry_after_ms
    if error.field_errors:
        body["field_errors"] = [
            {"path": item.path, "code": item.code, "message": item.message}
            for item in error.field_errors
        ]
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if error.retryable and error.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, math.ceil(error.retry_after_ms / 1000)))
    return JSONResponse({"error": body}, status_code=error.status_code, headers=headers)


def _invalid_token(request_id: str) -> ModelRequestError:
    return ModelRequestError("invalid_token", 401, "Authentication failed.", request_id)


def _body_too_large(request_id: str) -> ModelRequestError:
    return ModelRequestError(
        "invalid_request", 400, "The request body is too large.", request_id
    )


def _unsupported_stream(request_id: str) -> ModelRequestError:
    return ModelRequestError(
        "unsupported_contract",
        400,
        "The requested stream contract is not supported.",
        request_id,
    )


def _error_request_id() -> str:
    return str(uuid.uuid4())

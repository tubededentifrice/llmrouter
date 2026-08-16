"""Bounded internal OpenRouter text adapter."""
# ruff: noqa: C901, EM101, PLR0911, PLR0912, PLR0913, PLR0915, PLR2004, TRY003, TRY301

from __future__ import annotations

import ipaddress
import json
import math
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, NoReturn, TypeGuard, cast
from urllib.parse import urlsplit

import httpx

from llmrouter_backend.accounting import UsageComponent, UsageUnit
from llmrouter_backend.configuration import RegisteredSchema
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ErrorScope,
    TerminalError,
    TerminalErrorClass,
)
from llmrouter_backend.routing import (
    AdapterPhase,
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    SafeFailureEvidence,
)

from .model import (
    ModelAdapterRequest,
    ModelOperation,
    ModelOutputEvent,
    ModelOutputEventKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from llmrouter_backend.credential_store import SecretLease
    from llmrouter_backend.routing import AdapterProgress, AttemptPlan

OPENROUTER_ADAPTER_TYPE: Final[str] = "openai_compatible.v1"
OPENROUTER_PROFILE: Final[str] = "openrouter"
OPENROUTER_ENDPOINT: Final[str] = "https://openrouter.ai/api/v1"
DEEPSEEK_V4_FLASH_WIRE_MODEL: Final[str] = "deepseek/deepseek-v4-flash"
OPENROUTER_SUPPORTED_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {ModelOperation.COMPLETE.value, ModelOperation.STREAM.value}
)
OPENROUTER_UNSUPPORTED_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "audio.transcribe",
        "chat.audio",
        "chat.file",
        "chat.image",
        "embedding.batch",
        "image.generate",
        "tool.request",
    }
)
OPENROUTER_INSTANCE_SCHEMA: Final[RegisteredSchema] = RegisteredSchema(
    "adapter.openai_compatible.settings",
    1,
    {
        "attribution_referer": str,
        "attribution_title": str,
        "profile": str,
        "supported_operations": list,
    },
    frozenset({"profile", "supported_operations"}),
)
OPENROUTER_ROUTE_SCHEMA: Final[RegisteredSchema] = RegisteredSchema(
    "adapter.openai_compatible.route",
    1,
    {},
)

_MAXIMUM_BODY_BYTES = 8_388_608
_MAXIMUM_EVENT_BYTES = 1_048_576
_MAXIMUM_OUTPUT_DELTA_BYTES = 262_144
_MAXIMUM_HEADER_COUNT = 100
_MAXIMUM_HEADER_BYTES = 32_768
_MAXIMUM_CREDENTIAL_BYTES = 16_384
_MAXIMUM_WIRE_MODEL_CHARACTERS = 500
_MAXIMUM_OUTPUT_BYTES = 8_388_608
_MAXIMUM_OUTPUT_EVENTS = 10_000
_MAXIMUM_ATTRIBUTION_TITLE_CHARACTERS = 100
_MAXIMUM_ATTRIBUTION_REFERER_CHARACTERS = 2_000
_SAFE_PROVIDER_MESSAGE = "The provider attempt did not complete."


@dataclass(slots=True)
class _ActiveOperation:
    stop: threading.Event
    submitted: bool = False
    response: httpx.Response | None = None


class _StoppedError(RuntimeError):
    """The local provider transport received one stop request."""


class _InvalidResponseError(RuntimeError):
    """The provider response did not match the bounded wire contract."""


class _SinkError(RuntimeError):
    """The provider-neutral output sink did not accept an event."""

    def __init__(self, usage: tuple[UsageComponent, ...]) -> None:
        """Keep only safe usage when output delivery is uncertain."""
        self.usage = usage
        super().__init__("output_sink_failed")


class OpenRouterAdapter:
    """Map bounded provider-neutral text calls to OpenRouter chat completions."""

    def __init__(
        self,
        *,
        requests: Callable[[AttemptPlan], ModelAdapterRequest],
        credentials: Callable[[AttemptPlan], SecretLease],
        output: Callable[[AttemptPlan, ModelOutputEvent], None],
        clock: Callable[[], datetime] | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_loopback_test_endpoint: bool = False,
    ) -> None:
        """Use injected scoped content, credentials, output, and transport."""
        self._requests = requests
        self._credentials = credentials
        self._output = output
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._allow_loopback_test_endpoint = allow_loopback_test_endpoint
        self._active: dict[str, _ActiveOperation] = {}
        self._active_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Stop local active I/O, then close the owned safe client."""
        with self._active_lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active.values())
            for operation in active:
                operation.stop.set()
            responses = tuple(
                operation.response
                for operation in active
                if operation.response is not None
            )
        for response in responses:
            with suppress(Exception):
                response.close()
        self._http.close()

    def execute(self, plan: AttemptPlan, progress: AdapterProgress) -> AdapterResult:
        """Run one bounded text completion or stream."""
        active = _ActiveOperation(threading.Event())
        unavailable_code: str | None = None
        with self._active_lock:
            if self._closed:
                unavailable_code = "adapter_closed"
            elif plan.attempt_id in self._active:
                unavailable_code = "duplicate_active_attempt"
            else:
                self._active[plan.attempt_id] = active
        if unavailable_code is not None:
            return _preflight_failure(
                plan,
                progress,
                TerminalErrorClass.ROUTER_INTERNAL,
                ErrorScope.LOGICAL_REQUEST,
                unavailable_code,
            )
        try:
            try:
                request = self._requests(plan)
            except Exception:  # noqa: BLE001
                return _preflight_failure(
                    plan,
                    progress,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "request_source_failed",
                )
            if not isinstance(request, ModelAdapterRequest):
                return _preflight_failure(
                    plan,
                    progress,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "invalid_adapter_request",
                )
            configuration_failure = _validate_plan(
                plan,
                request,
                allow_loopback=self._allow_loopback_test_endpoint,
            )
            if configuration_failure is not None:
                _complete_preflight(progress)
                return configuration_failure
            try:
                body = _request_body(plan, request)
            except (UnicodeEncodeError, ValueError):
                return _preflight_failure(
                    plan,
                    progress,
                    TerminalErrorClass.INCOMPATIBLE_REQUEST,
                    ErrorScope.LOGICAL_REQUEST,
                    "invalid_adapter_request",
                )
            if active.stop.is_set():
                return _stopped_before_submit(plan, progress)
            try:
                lease = self._credentials(plan)
            except Exception:  # noqa: BLE001
                return _preflight_failure(
                    plan,
                    progress,
                    TerminalErrorClass.AUTHENTICATION,
                    ErrorScope.CREDENTIAL,
                    "credential_unavailable",
                )
            try:
                if (
                    lease.credential_id != plan.credential_id
                    or lease.generation != plan.credential_generation
                ):
                    return _preflight_failure(
                        plan,
                        progress,
                        TerminalErrorClass.AUTHENTICATION,
                        ErrorScope.CREDENTIAL,
                        "credential_generation_mismatch",
                    )
                try:
                    credential = bytes(lease.read(now=self._clock()))
                except Exception:  # noqa: BLE001
                    return _preflight_failure(
                        plan,
                        progress,
                        TerminalErrorClass.AUTHENTICATION,
                        ErrorScope.CREDENTIAL,
                        "credential_unavailable",
                    )
                if not _valid_credential(credential):
                    return _preflight_failure(
                        plan,
                        progress,
                        TerminalErrorClass.AUTHENTICATION,
                        ErrorScope.CREDENTIAL,
                        "credential_invalid",
                    )
                if active.stop.is_set():
                    return _stopped_before_submit(plan, progress)
                return self._send(
                    plan=plan,
                    request=request,
                    body=body,
                    credential=credential,
                    active=active,
                    progress=progress,
                )
            finally:
                lease.close()
        finally:
            response: httpx.Response | None = None
            with self._active_lock:
                current = self._active.get(plan.attempt_id)
                if current is active:
                    response = current.response
                    del self._active[plan.attempt_id]
            if response is not None:
                response.close()

    def cancel(self, plan: AttemptPlan) -> AdapterStopEvidence:
        """Close local I/O without claiming that upstream work stopped."""
        with self._active_lock:
            active = self._active.get(plan.attempt_id)
            if active is not None:
                active.stop.set()
                response = active.response
                confirmed_stopped = not active.submitted
            else:
                response = None
                confirmed_stopped = False
        safe_code = "local_transport_not_active"
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                safe_code = "local_transport_close_failed"
            else:
                safe_code = "local_transport_closed"
        elif confirmed_stopped:
            safe_code = "local_stop_before_submit"
        elif active is not None:
            safe_code = "local_stop_requested"
        return AdapterStopEvidence(
            plan.attempt_id,
            supported=True,
            stop_requested=True,
            confirmed_stopped=confirmed_stopped,
            safe_code=safe_code,
        )

    def _send(
        self,
        *,
        plan: AttemptPlan,
        request: ModelAdapterRequest,
        body: bytes,
        credential: bytes,
        active: _ActiveOperation,
        progress: AdapterProgress,
    ) -> AdapterResult:
        headers = _request_headers(plan.instance_settings, credential)
        timeout = httpx.Timeout(
            connect=plan.timeouts.connect_ms / 1_000,
            read=max(plan.timeouts.first_byte_ms, plan.timeouts.idle_ms) / 1_000,
            write=plan.timeouts.connect_ms / 1_000,
            pool=plan.timeouts.connect_ms / 1_000,
        )
        url = f"{plan.endpoint.rstrip('/')}/chat/completions"
        try:
            with self._active_lock:
                if (
                    self._active.get(plan.attempt_id) is not active
                    or active.stop.is_set()
                ):
                    can_submit = False
                else:
                    active.submitted = True
                    can_submit = True
            if not can_submit:
                raise _StoppedError
            progress(AdapterPhase.CONNECTED)
            with self._http.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=timeout,
            ) as response:
                with self._active_lock:
                    if self._active.get(plan.attempt_id) is active:
                        active.response = response
                if active.stop.is_set():
                    raise _StoppedError
                _validate_response_headers(
                    response, validate_content_length=response.status_code < 400
                )
                progress(AdapterPhase.FIRST_BYTE)
                if response.status_code >= 400:
                    progress(AdapterPhase.PROGRESS)
                    return _http_failure(plan, response)
                _validate_content_type(response, request.operation)
                if request.operation is ModelOperation.STREAM:
                    return self._read_stream(plan, response, active, progress)
                payload = _read_bounded(response, active, progress)
                return self._read_completion(plan, payload)
        except _StoppedError:
            return _failure(
                plan,
                TerminalErrorClass.UNCERTAIN_EFFECT,
                ErrorScope.LOGICAL_REQUEST,
                "local_stop_unconfirmed",
                outcome=AttemptOutcome.UNCERTAIN,
                usage=_request_usage(),
            )
        except _SinkError as error:
            return _failure(
                plan,
                TerminalErrorClass.UNCERTAIN_EFFECT,
                ErrorScope.LOGICAL_REQUEST,
                "output_sink_failed",
                outcome=AttemptOutcome.UNCERTAIN,
                usage=error.usage,
            )
        except httpx.TimeoutException:
            if active.stop.is_set():
                return _failure(
                    plan,
                    TerminalErrorClass.UNCERTAIN_EFFECT,
                    ErrorScope.LOGICAL_REQUEST,
                    "local_stop_unconfirmed",
                    outcome=AttemptOutcome.UNCERTAIN,
                    usage=_request_usage(),
                )
            return _failure(
                plan,
                TerminalErrorClass.TIMEOUT,
                ErrorScope.ATTEMPT,
                "provider_timeout",
                usage=_request_usage(),
            )
        except httpx.HTTPError:
            if active.stop.is_set():
                return _failure(
                    plan,
                    TerminalErrorClass.UNCERTAIN_EFFECT,
                    ErrorScope.LOGICAL_REQUEST,
                    "local_stop_unconfirmed",
                    outcome=AttemptOutcome.UNCERTAIN,
                    usage=_request_usage(),
                )
            return _failure(
                plan,
                TerminalErrorClass.TRANSPORT,
                ErrorScope.ATTEMPT,
                "provider_transport_error",
                usage=_request_usage(),
            )
        except (UnicodeEncodeError, _InvalidResponseError):
            return _failure(
                plan,
                TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
                ErrorScope.PROVIDER_MODEL_ROUTE,
                "invalid_provider_response",
                usage=_request_usage(),
            )
        except RuntimeError:
            if active.stop.is_set():
                return _failure(
                    plan,
                    TerminalErrorClass.UNCERTAIN_EFFECT,
                    ErrorScope.LOGICAL_REQUEST,
                    "local_stop_unconfirmed",
                    outcome=AttemptOutcome.UNCERTAIN,
                    usage=_request_usage(),
                )
            return _failure(
                plan,
                TerminalErrorClass.ROUTER_INTERNAL,
                ErrorScope.LOGICAL_REQUEST,
                "adapter_runtime_error",
                usage=_request_usage(),
            )

    def _read_completion(self, plan: AttemptPlan, payload: bytes) -> AdapterResult:
        root = _json_object(payload, maximum_bytes=_MAXIMUM_BODY_BYTES)
        choices = root.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise _InvalidResponseError
        choice = _mapping(choices[0])
        message = None if choice is None else _mapping(choice.get("message"))
        content = None if message is None else message.get("content")
        refusal = None if message is None else message.get("refusal")
        finish_reason = None if choice is None else choice.get("finish_reason")
        if finish_reason == "content_filter" or (
            isinstance(refusal, str) and bool(refusal)
        ):
            return _failure(
                plan,
                TerminalErrorClass.POLICY,
                ErrorScope.PROVIDER_MODEL_ROUTE,
                "provider_policy_refusal",
                usage=_usage(root.get("usage"), required=False),
            )
        if refusal is not None and not isinstance(refusal, str):
            raise _InvalidResponseError
        usage = _usage(root.get("usage"), required=True)
        if (
            not isinstance(content, str)
            or not content
            or len(content.encode("utf-8")) > _MAXIMUM_OUTPUT_BYTES
            or finish_reason not in {"stop", "length"}
        ):
            raise _InvalidResponseError
        for delta in _text_chunks(content):
            self._emit(
                plan,
                ModelOutputEvent(ModelOutputEventKind.DELTA, delta),
                usage=usage,
            )
        self._emit(plan, ModelOutputEvent(ModelOutputEventKind.COMPLETED), usage=usage)
        return AdapterResult(AttemptOutcome.SUCCEEDED, usage=usage)

    def _read_stream(
        self,
        plan: AttemptPlan,
        response: httpx.Response,
        active: _ActiveOperation,
        progress: AdapterProgress,
    ) -> AdapterResult:
        output_bytes = 0
        output_events = 0
        emitted = False
        done = False
        terminal = False
        usage: tuple[UsageComponent, ...] | None = None
        try:
            for data in _sse_data(response, active, progress):
                if done:
                    raise _InvalidResponseError
                if data == b"[DONE]":
                    done = True
                    continue
                root = _json_object(data, maximum_bytes=_MAXIMUM_EVENT_BYTES)
                if "error" in root:
                    return _stream_provider_failure(
                        plan, root["error"], emitted=emitted, usage=usage
                    )
                if "usage" in root:
                    reported_usage = _usage(root.get("usage"), required=True)
                    if usage is not None and usage != reported_usage:
                        raise _InvalidResponseError
                    usage = reported_usage
                choices = root.get("choices")
                if choices == [] and "usage" in root:
                    continue
                if not isinstance(choices, list) or len(choices) != 1:
                    raise _InvalidResponseError
                choice = _mapping(choices[0])
                delta = None if choice is None else _mapping(choice.get("delta"))
                content = None if delta is None else delta.get("content")
                refusal = None if delta is None else delta.get("refusal")
                finish_reason = None if choice is None else choice.get("finish_reason")
                if finish_reason == "content_filter" or (
                    isinstance(refusal, str) and bool(refusal)
                ):
                    return _failure(
                        plan,
                        TerminalErrorClass.POLICY,
                        ErrorScope.PROVIDER_MODEL_ROUTE,
                        "provider_policy_refusal",
                        outcome=(
                            AttemptOutcome.INTERRUPTED
                            if emitted
                            else AttemptOutcome.FAILED
                        ),
                        usage=_request_usage() if usage is None else usage,
                    )
                if refusal is not None and not isinstance(refusal, str):
                    raise _InvalidResponseError
                if finish_reason is not None and not isinstance(finish_reason, str):
                    raise _InvalidResponseError
                if finish_reason is not None:
                    if terminal or finish_reason not in {"stop", "length"}:
                        raise _InvalidResponseError
                    terminal = True
                if content is None:
                    continue
                if not isinstance(content, str):
                    raise _InvalidResponseError
                if not content:
                    continue
                if terminal and finish_reason is None:
                    raise _InvalidResponseError
                try:
                    encoded = content.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise _InvalidResponseError from error
                if len(encoded) > _MAXIMUM_OUTPUT_DELTA_BYTES:
                    raise _InvalidResponseError
                output_bytes += len(encoded)
                output_events += 1
                if (
                    output_bytes > _MAXIMUM_OUTPUT_BYTES
                    or output_events > _MAXIMUM_OUTPUT_EVENTS
                ):
                    raise _InvalidResponseError
                self._emit(
                    plan,
                    ModelOutputEvent(ModelOutputEventKind.DELTA, content),
                    usage=_request_usage() if usage is None else usage,
                )
                emitted = True
        except _InvalidResponseError:
            if emitted:
                return _failure(
                    plan,
                    TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
                    ErrorScope.PROVIDER_MODEL_ROUTE,
                    "invalid_stream_response",
                    outcome=AttemptOutcome.INTERRUPTED,
                    usage=_request_usage() if usage is None else usage,
                )
            raise
        except httpx.HTTPError:
            if active.stop.is_set():
                raise _StoppedError from None
            if emitted:
                return _failure(
                    plan,
                    TerminalErrorClass.TRANSPORT,
                    ErrorScope.ATTEMPT,
                    "stream_interrupted",
                    outcome=AttemptOutcome.INTERRUPTED,
                    usage=_request_usage() if usage is None else usage,
                )
            raise
        if not done or not terminal or not emitted or usage is None:
            if emitted:
                return _failure(
                    plan,
                    TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
                    ErrorScope.PROVIDER_MODEL_ROUTE,
                    "invalid_stream_terminal",
                    outcome=AttemptOutcome.INTERRUPTED,
                    usage=_request_usage() if usage is None else usage,
                )
            raise _InvalidResponseError
        self._emit(plan, ModelOutputEvent(ModelOutputEventKind.COMPLETED), usage=usage)
        return AdapterResult(AttemptOutcome.SUCCEEDED, usage=usage)

    def _emit(
        self,
        plan: AttemptPlan,
        event: ModelOutputEvent,
        *,
        usage: tuple[UsageComponent, ...],
    ) -> None:
        try:
            self._output(plan, event)
        except Exception as error:
            raise _SinkError(usage) from error


def openrouter_registered_schemas() -> tuple[RegisteredSchema, ...]:
    """Return the closed internal schemas for the OpenRouter profile."""
    return (OPENROUTER_INSTANCE_SCHEMA, OPENROUTER_ROUTE_SCHEMA)


def _validate_plan(
    plan: AttemptPlan,
    request: ModelAdapterRequest,
    *,
    allow_loopback: bool,
) -> AdapterResult | None:
    if plan.adapter_type != OPENROUTER_ADAPTER_TYPE:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            "adapter_type_mismatch",
        )
    if not _valid_wire_model(plan.wire_model):
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            "invalid_wire_model",
        )
    if not _valid_endpoint(plan.endpoint, allow_loopback=allow_loopback):
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_INSTANCE,
            "endpoint_not_allowed",
        )
    settings = plan.instance_settings
    if set(settings) - {
        "attribution_referer",
        "attribution_title",
        "profile",
        "supported_operations",
    } or set(settings) < {"profile", "supported_operations"}:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_INSTANCE,
            "invalid_instance_settings",
        )
    if settings.get("profile") != OPENROUTER_PROFILE:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_INSTANCE,
            "invalid_provider_profile",
        )
    operations = settings.get("supported_operations")
    if not isinstance(operations, tuple) or (
        not operations
        or any(not isinstance(item, str) for item in operations)
        or len(set(operations)) != len(operations)
        or not set(operations) <= OPENROUTER_SUPPORTED_CAPABILITIES
    ):
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_INSTANCE,
            "invalid_supported_operations",
        )
    if plan.route_settings:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            "invalid_route_settings",
        )
    operation = request.operation.value
    if operation not in operations or operation not in plan.capabilities:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            "unsupported_capability",
        )
    if any(item not in OPENROUTER_SUPPORTED_CAPABILITIES for item in plan.capabilities):
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            "unsupported_capability_declaration",
        )
    try:
        _request_headers(settings, b"x")
    except ValueError:
        return _failure(
            plan,
            TerminalErrorClass.INCOMPATIBLE_REQUEST,
            ErrorScope.PROVIDER_INSTANCE,
            "invalid_attribution_settings",
        )
    return None


def _valid_endpoint(value: str, *, allow_loopback: bool) -> bool:
    if value == OPENROUTER_ENDPOINT:
        return True
    if not allow_loopback:
        return False
    try:
        endpoint = urlsplit(value)
        host = endpoint.hostname
        port = endpoint.port
    except ValueError:
        return False
    if (
        endpoint.scheme not in {"http", "https"}
        or host is None
        or port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or not endpoint.path.startswith("/")
        or ".." in endpoint.path.split("/")
    ):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def _valid_wire_model(value: str) -> bool:
    if not 3 <= len(value) <= _MAXIMUM_WIRE_MODEL_CHARACTERS or value.count("/") != 1:
        return False
    provider, model = value.split("/", maxsplit=1)
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:"
    )
    return bool(
        provider and model and set(provider) <= allowed and set(model) <= allowed
    )


def _valid_credential(value: bytes) -> bool:
    return 1 <= len(value) <= _MAXIMUM_CREDENTIAL_BYTES and all(
        33 <= item <= 126 for item in value
    )


def _request_headers(
    settings: Mapping[str, object], credential: bytes
) -> dict[str, str]:
    try:
        secret = credential.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("The credential is invalid.") from error
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    referer = settings.get("attribution_referer")
    if referer is not None:
        if not isinstance(referer, str) or not _valid_attribution_referer(referer):
            raise ValueError("The attribution origin is invalid.")
        headers["HTTP-Referer"] = referer
    title = settings.get("attribution_title")
    if title is not None:
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= _MAXIMUM_ATTRIBUTION_TITLE_CHARACTERS
            or any(not " " <= item <= "~" for item in title)
        ):
            raise ValueError("The attribution title is invalid.")
        headers["X-Title"] = title
    if (
        sum(len(key) + len(value) for key, value in headers.items())
        > _MAXIMUM_HEADER_BYTES
    ):
        raise ValueError("The request headers are too large.")
    return headers


def _valid_attribution_referer(value: str) -> bool:
    if len(value) > _MAXIMUM_ATTRIBUTION_REFERER_CHARACTERS or any(
        not "!" <= item <= "~" for item in value
    ):
        return False
    try:
        endpoint = urlsplit(value)
        _ = endpoint.port
    except ValueError:
        return False
    return bool(
        endpoint.scheme == "https"
        and endpoint.hostname
        and endpoint.username is None
        and endpoint.password is None
        and not endpoint.query
        and not endpoint.fragment
    )


def _request_body(plan: AttemptPlan, request: ModelAdapterRequest) -> bytes:
    value: dict[str, object] = {
        "messages": [
            {"role": item.role.value, "content": item.content}
            for item in request.messages
        ],
        "model": plan.wire_model,
        "max_tokens": request.max_output_units,
        "stream": request.operation is ModelOperation.STREAM,
    }
    if request.temperature is not None:
        value["temperature"] = _wire_temperature(request.temperature)
    if request.operation is ModelOperation.STREAM:
        value["stream_options"] = {"include_usage": True}
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > _MAXIMUM_BODY_BYTES:
        raise ValueError
    return body


def _wire_temperature(value: Decimal) -> int | float:
    """Convert one bounded exact control to a finite JSON number."""
    if value == value.to_integral_value():
        return int(value)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("The model temperature is not finite.")
    return result


def _validate_response_headers(
    response: httpx.Response, *, validate_content_length: bool
) -> None:
    items = response.headers.multi_items()
    if (
        len(items) > _MAXIMUM_HEADER_COUNT
        or sum(len(key) + len(value) for key, value in items) > _MAXIMUM_HEADER_BYTES
    ):
        raise _InvalidResponseError
    content_length = response.headers.get("content-length")
    if (
        validate_content_length
        and content_length is not None
        and (
            not content_length.isdecimal() or int(content_length) > _MAXIMUM_BODY_BYTES
        )
    ):
        raise _InvalidResponseError
    if response.history:
        raise _InvalidResponseError


def _validate_content_type(response: httpx.Response, operation: ModelOperation) -> None:
    value = response.headers.get("content-type")
    if value is None or len(value) > 200:
        raise _InvalidResponseError
    media_type = value.partition(";")[0].strip().casefold()
    expected = (
        "text/event-stream"
        if operation is ModelOperation.STREAM
        else "application/json"
    )
    if media_type != expected:
        raise _InvalidResponseError


def _read_bounded(
    response: httpx.Response,
    active: _ActiveOperation,
    progress: AdapterProgress,
) -> bytes:
    body = bytearray()
    saw_chunk = False
    for chunk in response.iter_bytes():
        if active.stop.is_set():
            raise _StoppedError
        if not chunk:
            continue
        saw_chunk = True
        if len(chunk) > _MAXIMUM_BODY_BYTES - len(body):
            raise _InvalidResponseError
        body.extend(chunk)
        progress(AdapterPhase.PROGRESS)
    if not saw_chunk:
        progress(AdapterPhase.PROGRESS)
    return bytes(body)


def _sse_data(
    response: httpx.Response,
    active: _ActiveOperation,
    progress: AdapterProgress,
) -> Iterator[bytes]:
    buffer = bytearray()
    data_lines: list[bytes] = []
    event_bytes = 0
    total_bytes = 0
    saw_chunk = False
    for chunk in response.iter_bytes():
        if active.stop.is_set():
            raise _StoppedError
        if not chunk:
            continue
        saw_chunk = True
        total_bytes += len(chunk)
        if total_bytes > _MAXIMUM_BODY_BYTES:
            raise _InvalidResponseError
        buffer.extend(chunk)
        progress(AdapterPhase.PROGRESS)
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            line = bytes(raw_line).removesuffix(b"\r")
            if not line:
                if data_lines:
                    yield b"\n".join(data_lines)
                data_lines = []
                event_bytes = 0
                continue
            event_bytes += len(line)
            if event_bytes > _MAXIMUM_EVENT_BYTES:
                raise _InvalidResponseError
            if line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                raise _InvalidResponseError
            data = line[5:]
            if data.startswith(b" "):
                data = data[1:]
            data_lines.append(data)
        if len(buffer) + event_bytes > _MAXIMUM_EVENT_BYTES:
            raise _InvalidResponseError
    if not saw_chunk:
        progress(AdapterPhase.PROGRESS)
    if buffer:
        line = bytes(buffer).removesuffix(b"\r")
        if not line.startswith(b"data:"):
            raise _InvalidResponseError
        data = line[5:]
        data_lines.append(data[1:] if data.startswith(b" ") else data)
    if data_lines:
        yield b"\n".join(data_lines)


def _json_object(payload: bytes, *, maximum_bytes: int) -> dict[str, object]:
    if not payload or len(payload) > maximum_bytes:
        raise _InvalidResponseError
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_json_constant,
            parse_float=_strict_json_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise _InvalidResponseError from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _InvalidResponseError
    return cast("dict[str, object]", value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The provider JSON contains a duplicate field.")
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> NoReturn:
    raise ValueError("The provider JSON contains a non-finite number.")


def _strict_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("The provider JSON contains a non-finite number.")
    return result


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return None
    return cast("Mapping[str, object]", value)


def _request_usage() -> tuple[UsageComponent, ...]:
    return (UsageComponent(UsageUnit.REQUEST, Decimal(1)),)


def _usage(value: object, *, required: bool) -> tuple[UsageComponent, ...]:
    result = list(_request_usage())
    usage = _mapping(value)
    if usage is None:
        if required:
            raise _InvalidResponseError
        return tuple(result)
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if not _valid_usage_integer(prompt_tokens) or not _valid_usage_integer(
        completion_tokens
    ):
        if required or prompt_tokens is not None or completion_tokens is not None:
            raise _InvalidResponseError
        return tuple(result)
    details = _mapping(usage.get("prompt_tokens_details"))
    cached_tokens = 0
    if details is not None:
        cached = details.get("cached_tokens")
        if _valid_usage_integer(cached):
            cached_tokens = cached
        elif cached is not None:
            raise _InvalidResponseError
    elif usage.get("prompt_tokens_details") is not None:
        raise _InvalidResponseError
    if cached_tokens > prompt_tokens:
        raise _InvalidResponseError
    result.extend(
        (
            UsageComponent(
                UsageUnit.INPUT_TOKEN, Decimal(prompt_tokens - cached_tokens)
            ),
            UsageComponent(UsageUnit.OUTPUT_TOKEN, Decimal(completion_tokens)),
        )
    )
    if details is not None and "cached_tokens" in details:
        result.append(UsageComponent(UsageUnit.CACHED_TOKEN, Decimal(cached_tokens)))
    return tuple(result)


def _valid_usage_integer(value: object) -> TypeGuard[int]:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**15
    )


def _text_chunks(value: str) -> Iterator[str]:
    chunk: list[str] = []
    size = 0
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if chunk and size + encoded_size > 262_144:
            yield "".join(chunk)
            chunk = []
            size = 0
        chunk.append(character)
        size += encoded_size
    if chunk:
        yield "".join(chunk)


def _http_failure(plan: AttemptPlan, response: httpx.Response) -> AdapterResult:
    status = response.status_code
    retry_after_ms = _retry_after_ms(response.headers.get("retry-after"))
    if status == 401:
        error_class = TerminalErrorClass.AUTHENTICATION
        scope = ErrorScope.CREDENTIAL
    elif status == 403:
        error_class = TerminalErrorClass.POLICY
        scope = ErrorScope.PROVIDER_INSTANCE
    elif status == 402:
        error_class = TerminalErrorClass.BUDGET
        scope = ErrorScope.PROVIDER_INSTANCE
    elif status == 408:
        error_class = TerminalErrorClass.TIMEOUT
        scope = ErrorScope.ATTEMPT
    elif status == 429:
        error_class = TerminalErrorClass.RATE_LIMIT
        scope = ErrorScope.PROVIDER_INSTANCE
    elif status in {400, 404, 413, 422}:
        error_class = TerminalErrorClass.INCOMPATIBLE_REQUEST
        scope = ErrorScope.PROVIDER_MODEL_ROUTE
    elif 400 <= status < 500:
        error_class = TerminalErrorClass.TRANSPORT
        scope = ErrorScope.ATTEMPT
    elif 500 <= status < 600:
        error_class = TerminalErrorClass.PROVIDER_UNAVAILABLE
        scope = ErrorScope.ATTEMPT
    else:
        error_class = TerminalErrorClass.TRANSPORT
        scope = ErrorScope.ATTEMPT
    return _failure(
        plan,
        error_class,
        scope,
        f"provider_http_{status}",
        provider_status=status,
        retry_after_ms=retry_after_ms,
        usage=(UsageComponent(UsageUnit.REQUEST, Decimal(1)),),
    )


def _stream_provider_failure(
    plan: AttemptPlan,
    value: object,
    *,
    emitted: bool,
    usage: tuple[UsageComponent, ...] | None,
) -> AdapterResult:
    error = _mapping(value)
    if error is None:
        raise _InvalidResponseError
    status = error.get("code")
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 400 <= status <= 599
    ):
        raise _InvalidResponseError
    metadata = _mapping(error.get("metadata"))
    error_type = None if metadata is None else metadata.get("error_type")
    if error_type is not None and not isinstance(error_type, str):
        raise _InvalidResponseError
    error_class, scope = _provider_error_classification(status, error_type)
    return _failure(
        plan,
        error_class,
        scope,
        "stream_provider_error",
        outcome=AttemptOutcome.INTERRUPTED if emitted else AttemptOutcome.FAILED,
        provider_status=status,
        usage=_request_usage() if usage is None else usage,
    )


def _provider_error_classification(
    status: int, error_type: str | None
) -> tuple[TerminalErrorClass, ErrorScope]:
    if error_type == "authentication" or status == 401:
        return TerminalErrorClass.AUTHENTICATION, ErrorScope.CREDENTIAL
    if error_type in {"permission_denied", "content_policy_violation", "refusal"}:
        scope = (
            ErrorScope.PROVIDER_INSTANCE
            if error_type == "permission_denied"
            else ErrorScope.PROVIDER_MODEL_ROUTE
        )
        return TerminalErrorClass.POLICY, scope
    if error_type == "payment_required" or status == 402:
        return TerminalErrorClass.BUDGET, ErrorScope.PROVIDER_INSTANCE
    if error_type == "rate_limit_exceeded" or status == 429:
        return TerminalErrorClass.RATE_LIMIT, ErrorScope.PROVIDER_INSTANCE
    if error_type == "timeout" or status in {408, 504}:
        return TerminalErrorClass.TIMEOUT, ErrorScope.ATTEMPT
    if error_type in {
        "provider_overloaded",
        "provider_unavailable",
        "server",
        "unmapped",
    }:
        return TerminalErrorClass.PROVIDER_UNAVAILABLE, ErrorScope.ATTEMPT
    if error_type in {
        "context_length_exceeded",
        "invalid_prompt",
        "invalid_request",
        "max_tokens_exceeded",
        "not_found",
        "payload_too_large",
        "precondition_failed",
        "string_too_long",
        "token_limit_exceeded",
        "unprocessable",
    } or status in {400, 404, 412, 413, 422}:
        return TerminalErrorClass.INCOMPATIBLE_REQUEST, ErrorScope.PROVIDER_MODEL_ROUTE
    if status == 403:
        return TerminalErrorClass.POLICY, ErrorScope.PROVIDER_INSTANCE
    if 500 <= status < 600:
        return TerminalErrorClass.PROVIDER_UNAVAILABLE, ErrorScope.ATTEMPT
    return TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT


def _retry_after_ms(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    seconds = int(value)
    if not 0 <= seconds <= 900:
        return None
    return seconds * 1_000


def _preflight_failure(
    plan: AttemptPlan,
    progress: AdapterProgress,
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    detail_code: str,
) -> AdapterResult:
    _complete_preflight(progress)
    return _failure(plan, error_class, scope, detail_code)


def _stopped_before_submit(
    plan: AttemptPlan, progress: AdapterProgress
) -> AdapterResult:
    _complete_preflight(progress)
    return _failure(
        plan,
        TerminalErrorClass.UNCERTAIN_EFFECT,
        ErrorScope.LOGICAL_REQUEST,
        "local_stop_before_submit",
        outcome=AttemptOutcome.UNCERTAIN,
    )


def _complete_preflight(progress: AdapterProgress) -> None:
    progress(AdapterPhase.CONNECTED)
    progress(AdapterPhase.FIRST_BYTE)
    progress(AdapterPhase.PROGRESS)


def _failure(
    plan: AttemptPlan,
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    detail_code: str,
    *,
    outcome: AttemptOutcome = AttemptOutcome.FAILED,
    provider_status: int | None = None,
    retry_after_ms: int | None = None,
    usage: tuple[UsageComponent, ...] = (),
) -> AdapterResult:
    safe_code = detail_code if detail_code.startswith("provider_http_") else None
    error = TerminalError(error_class, scope, _SAFE_PROVIDER_MESSAGE, safe_code)
    identities = {
        ErrorScope.ATTEMPT: plan.attempt_id,
        ErrorScope.PROVIDER_MODEL_ROUTE: plan.provider_model_route_id,
        ErrorScope.PROVIDER_INSTANCE: plan.provider_instance_id,
        ErrorScope.CREDENTIAL: plan.credential_id,
        ErrorScope.ASSIGNMENT_CANDIDATE: plan.provider_model_route_id,
        ErrorScope.LOGICAL_REQUEST: plan.request_id,
    }
    failure = AttemptFailure(
        error,
        identities[scope],
        SafeFailureEvidence(provider_status, retry_after_ms, detail_code),
    )
    return AdapterResult(outcome, failure, usage)

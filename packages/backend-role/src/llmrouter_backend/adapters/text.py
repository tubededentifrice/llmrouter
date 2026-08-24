"""Bounded text adapters for the accepted provider protocols."""
# ruff: noqa: C901, D107, EM101, PLR0912, PLR0915, PLR2004, SIM117, TRY004, TRY301

from __future__ import annotations

import base64
import ipaddress
import json
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from opendle import CallFailurePhase

from llmrouter_backend.accounting import UsageAmount
from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping

type OpenAIAdapterName = Literal["openai", "openai_compatible", "openrouter", "custom"]

_OPENAI_ENDPOINT = "https://api.openai.com/v1"
_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
_MAXIMUM_REQUEST_BYTES = 2 * 1024 * 1024 + 4 * ((50 * 1024 * 1024 + 2) // 3) + 8 * 128
_MAXIMUM_RESPONSE_BYTES = 8 * 1024 * 1024
_MAXIMUM_EVENT_BYTES = 1024 * 1024
_MAXIMUM_HEADERS = 100
_MAXIMUM_HEADER_BYTES = 32 * 1024
_MAXIMUM_TOOL_ARGUMENT_BYTES = 1024 * 1024
_MAXIMUM_TOOL_CALLS = 128
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
_OPENAI_USAGE_UNITS = frozenset(
    {"input_token", "output_token", "cached_input_token", "request"}
)
_OLLAMA_USAGE_UNITS = frozenset({"input_token", "output_token", "request"})


class OpenAITextAdapter:
    """Map native model calls to one accepted OpenAI-compatible profile."""

    usage_units = _OPENAI_USAGE_UNITS

    def __init__(
        self,
        adapter_name: OpenAIAdapterName,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._adapter_name = adapter_name
        self._transport = transport

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare the complete priced unit set for one text operation."""
        return self.usage_units if operation.kind == "model" else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Run one bounded provider attempt without redirects or environment trust."""
        if request.route.adapter != self._adapter_name or request.kind != "model":
            raise _failure("incompatible")
        if self._adapter_name in {"openai", "openrouter"} and not request.credential:
            raise _failure("authentication")
        authorization: str | None = None
        if request.credential is not None:
            try:
                authorization = _bearer_authorization(request.credential)
            except ValueError:
                raise _failure("authentication") from None
        try:
            _validate_input_media(request)
            endpoint = _openai_endpoint(request)
            body = _openai_request(request)
        except (
            KeyError,
            StopIteration,
            TypeError,
            UnicodeError,
            ValueError,
            RecursionError,
        ):
            raise _failure("incompatible") from None
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if request.streaming else "application/json",
        }
        if authorization is not None:
            headers["Authorization"] = authorization
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint.rstrip('/')}/chat/completions",
                    content=body,
                    headers=headers,
                ) as response:
                    try:
                        _validate_headers(response)
                        if response.status_code >= 300:
                            raise _http_failure(response.status_code)
                        if request.streaming:
                            _require_content_type(response, "text/event-stream")
                            async for event in _openai_stream(response, request):
                                yield event
                        else:
                            _require_content_type(response, "application/json")
                            payload = await _read_response(response)
                            output, usage = _openai_completion(payload, request)
                            yield output
                            yield ProviderCompleted(usage)
                    except ProviderFailureError as error:
                        raise _failure_with_available_usage(error) from None
                    except httpx.TimeoutException:
                        raise _failure("timeout", usage=_request_usage()) from None
                    except httpx.HTTPError:
                        raise _failure("transport", usage=_request_usage()) from None
                    except (
                        KeyError,
                        TypeError,
                        UnicodeError,
                        ValueError,
                        RecursionError,
                    ):
                        raise _failure(
                            "invalid_response", usage=_request_usage()
                        ) from None
        except ProviderFailureError:
            raise
        except httpx.TimeoutException:
            raise _failure("timeout") from None
        except httpx.HTTPError:
            raise _failure("transport") from None
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure("invalid_response") from None


class OllamaTextAdapter:
    """Map native model calls to the bounded native Ollama chat protocol."""

    usage_units = _OLLAMA_USAGE_UNITS

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare Ollama token and request units for one text operation."""
        return self.usage_units if operation.kind == "model" else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Run one native Ollama chat attempt with an optional bearer credential."""
        if request.route.adapter != "ollama" or request.kind != "model":
            raise _failure("incompatible")
        try:
            _validate_input_media(request)
            endpoint = _route_endpoint(request.route.endpoint, loopback_required=False)
            body = _ollama_request(request)
        except (
            KeyError,
            StopIteration,
            TypeError,
            UnicodeError,
            ValueError,
            RecursionError,
        ):
            raise _failure("incompatible") from None
        headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}
        if request.credential is not None:
            try:
                headers["Authorization"] = _bearer_authorization(request.credential)
            except ValueError:
                raise _failure("authentication") from None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint.rstrip('/')}/api/chat",
                    content=body,
                    headers=headers,
                ) as response:
                    try:
                        _validate_headers(response)
                        if response.status_code >= 300:
                            raise _http_failure(response.status_code)
                        if request.streaming:
                            _require_one_content_type(
                                response,
                                {"application/x-ndjson", "application/json"},
                            )
                            async for event in _ollama_stream(response, request):
                                yield event
                        else:
                            _require_content_type(response, "application/json")
                            payload = await _read_response(response)
                            output, usage = _ollama_completion(payload, request)
                            yield output
                            yield ProviderCompleted(usage)
                    except ProviderFailureError as error:
                        raise _failure_with_available_usage(error) from None
                    except httpx.TimeoutException:
                        raise _failure("timeout", usage=_request_usage()) from None
                    except httpx.HTTPError:
                        raise _failure("transport", usage=_request_usage()) from None
                    except (
                        KeyError,
                        TypeError,
                        UnicodeError,
                        ValueError,
                        RecursionError,
                    ):
                        raise _failure(
                            "invalid_response", usage=_request_usage()
                        ) from None
        except ProviderFailureError:
            raise
        except httpx.TimeoutException:
            raise _failure("timeout") from None
        except httpx.HTTPError:
            raise _failure("transport") from None
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure("invalid_response") from None


def _openai_endpoint(request: ProviderAttemptRequest) -> str:
    if request.route.adapter == "openai":
        if request.route.endpoint is not None:
            raise ValueError
        return _OPENAI_ENDPOINT
    if request.route.adapter == "openrouter":
        if request.route.endpoint is not None:
            raise ValueError
        return _OPENROUTER_ENDPOINT
    return _route_endpoint(request.route.endpoint, loopback_required=False)


def _route_endpoint(value: str | None, *, loopback_required: bool) -> str:
    if value is None or len(value) > 4096 or value.strip() != value:
        raise ValueError
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError from None
    host = parsed.hostname
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        not host
        or (not loopback and not _valid_public_hostname(host))
        or (loopback_required and not loopback)
        or parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or (port is not None and not 1 <= port <= 65_535)
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError
    return value


def _valid_public_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        return bool(
            len(labels) >= 2
            and len(host) <= 253
            and all(
                1 <= len(label) <= 63
                and label.isascii()
                and label.lower() == label
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in labels
            )
        )
    return False


def _native_body(request: ProviderAttemptRequest) -> Mapping[str, object]:
    if len(request.request_json.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError
    value = _load_json(request.request_json)
    if not isinstance(value, dict):
        raise ValueError
    return cast("Mapping[str, object]", value)


def _validate_input_media(request: ProviderAttemptRequest) -> None:
    if tuple(len(item.body) for item in request.input_media) != (
        request.requirements.input_image_sizes
    ) or any(
        type(item.body) is not bytes
        or item.role != "input"
        or item.media_type not in {"image/jpeg", "image/png", "image/webp"}
        or not 1 <= len(item.body) <= 20 * 1024 * 1024
        for item in request.input_media
    ):
        raise ValueError


def _openai_request(request: ProviderAttemptRequest) -> bytes:
    native = _native_body(request)
    value: dict[str, object] = {
        "model": request.route.provider_model_name,
        "messages": _openai_messages(native, request),
        "stream": request.streaming,
    }
    if request.streaming:
        value["stream_options"] = {"include_usage": True}
    _copy_optional(native, value, {"temperature": "temperature"})
    output_limit = native.get("output_limit")
    if output_limit is not None:
        value[
            "max_completion_tokens"
            if request.route.adapter == "openai"
            else "max_tokens"
        ] = output_limit
    tools = native.get("tools")
    if tools is not None:
        value["tools"] = _openai_tools(tools)
    output_format = native.get("output_format")
    if output_format is not None:
        value["response_format"] = _openai_output_format(output_format)
    if request.route.provider_reasoning_value is not None:
        if request.route.adapter == "openrouter":
            value["reasoning"] = {
                "effort": request.route.provider_reasoning_value,
            }
        else:
            value["reasoning_effort"] = request.route.provider_reasoning_value
    return _dump_request(value)


def _openai_messages(
    native: Mapping[str, object], request: ProviderAttemptRequest
) -> list[dict[str, object]]:
    messages = native.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError
    media = iter(request.input_media)
    output: list[dict[str, object]] = []
    for source in messages:
        if not isinstance(source, dict) or not isinstance(source.get("role"), str):
            raise ValueError
        role = source["role"]
        content = source.get("content")
        if role == "system":
            if not isinstance(content, str) or not content:
                raise ValueError
            output.append({"role": "system", "content": content})
        elif role == "assistant":
            output.append(_openai_assistant(content))
        elif role == "user":
            _append_openai_user(output, content, media)
        else:
            raise ValueError
    try:
        next(media)
    except StopIteration:
        return output
    raise ValueError


def _append_openai_user(
    output: list[dict[str, object]], content: object, media: Iterator[Any]
) -> None:
    if not isinstance(content, list) or not content:
        raise ValueError
    current: list[dict[str, object]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            current.append({"type": "text", "text": part["text"]})
        elif part_type == "image":
            item = next(media)
            current.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:"
                        + item.media_type
                        + ";base64,"
                        + base64.b64encode(item.body).decode("ascii")
                    },
                }
            )
        elif part_type == "tool_result" and all(
            isinstance(part.get(field), str)
            for field in ("tool_call_id", "result_json")
        ):
            if current:
                output.append({"role": "user", "content": current})
                current = []
            output.append(
                {
                    "role": "tool",
                    "tool_call_id": part["tool_call_id"],
                    "content": part["result_json"],
                }
            )
        else:
            raise ValueError
    if current:
        output.append({"role": "user", "content": current})


def _openai_assistant(content: object) -> dict[str, object]:
    if not isinstance(content, list) or not content:
        raise ValueError
    text: list[str] = []
    calls: list[dict[str, object]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            text.append(cast("str", part["text"]))
        elif part.get("type") == "tool_call" and all(
            isinstance(part.get(field), str)
            for field in ("id", "name", "arguments_json")
        ):
            arguments = _load_json(cast("str", part["arguments_json"]))
            if not isinstance(arguments, dict):
                raise ValueError
            calls.append(
                {
                    "id": part["id"],
                    "type": "function",
                    "function": {
                        "name": part["name"],
                        "arguments": part["arguments_json"],
                    },
                }
            )
        else:
            raise ValueError
    result: dict[str, object] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        result["tool_calls"] = calls
    return result


def _openai_tools(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str)
            for field in ("name", "description", "input_schema_json")
        ):
            raise ValueError
        schema = _load_json(cast("str", item["input_schema_json"]))
        if not isinstance(schema, dict):
            raise ValueError
        result.append(
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": schema,
                },
            }
        )
    return result


def _openai_output_format(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("type") != "json_schema":
        raise ValueError
    schema = _load_json(cast("str", value.get("schema_json")))
    if not isinstance(schema, dict):
        raise ValueError
    return {
        "type": "json_schema",
        "json_schema": {"name": "router_response", "strict": True, "schema": schema},
    }


def _ollama_request(request: ProviderAttemptRequest) -> bytes:
    native = _native_body(request)
    value: dict[str, object] = {
        "model": request.route.provider_model_name,
        "messages": _ollama_messages(native, request),
        "stream": request.streaming,
    }
    options: dict[str, object] = {}
    _copy_optional(
        native, options, {"temperature": "temperature", "output_limit": "num_predict"}
    )
    if options:
        value["options"] = options
    tools = native.get("tools")
    if tools is not None:
        value["tools"] = _openai_tools(tools)
    output_format = native.get("output_format")
    if output_format is not None:
        if (
            not isinstance(output_format, dict)
            or output_format.get("type") != "json_schema"
        ):
            raise ValueError
        schema = _load_json(cast("str", output_format.get("schema_json")))
        if not isinstance(schema, dict):
            raise ValueError
        value["format"] = schema
    if request.route.provider_reasoning_value is not None:
        value["think"] = request.route.provider_reasoning_value
    return _dump_request(value)


def _ollama_messages(
    native: Mapping[str, object], request: ProviderAttemptRequest
) -> list[dict[str, object]]:
    # Ollama accepts the same assistant/tool shape with image bytes on messages.
    openai_messages = _openai_messages(native, request)
    result: list[dict[str, object]] = []
    for message in openai_messages:
        role = message["role"]
        if role == "user" and isinstance(message.get("content"), list):
            texts: list[str] = []
            images: list[str] = []
            for part in cast("list[dict[str, object]]", message["content"]):
                if part.get("type") == "text":
                    texts.append(cast("str", part["text"]))
                else:
                    image_url = cast("dict[str, str]", part["image_url"])["url"]
                    images.append(image_url.partition(",")[2])
            converted: dict[str, object] = {"role": "user", "content": "".join(texts)}
            if images:
                converted["images"] = images
            result.append(converted)
        elif role == "tool":
            result.append({"role": "tool", "content": message["content"]})
        elif role == "assistant" and message.get("tool_calls") is not None:
            converted = dict(message)
            converted_calls: list[dict[str, object]] = []
            for call in cast("list[dict[str, object]]", message["tool_calls"]):
                function = cast("dict[str, object]", call["function"])
                arguments = _load_json(cast("str", function["arguments"]))
                if not isinstance(arguments, dict):
                    raise ValueError
                converted_calls.append(
                    {
                        "function": {
                            "name": function["name"],
                            "arguments": arguments,
                        }
                    }
                )
            converted["tool_calls"] = converted_calls
            result.append(converted)
        else:
            result.append(message)
    return result


def _openai_completion(
    payload: bytes, request: ProviderAttemptRequest
) -> tuple[ProviderOutput, tuple[UsageAmount, ...]]:
    root = _json_object(payload)
    usage = _openai_usage(root.get("usage"))
    try:
        choices = root.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            raise _failure("refusal")
        if finish_reason not in {"stop", "length", "tool_calls"}:
            raise ValueError
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError
        if message.get("refusal") not in {None, ""}:
            raise _failure("refusal")
        return _provider_output(message, request), usage
    except ProviderFailureError as error:
        raise _failure(error.failure_class, usage=usage, phase=error.phase) from None
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise _failure("invalid_response", usage=usage) from None


def _provider_output(
    message: Mapping[str, object], request: ProviderAttemptRequest
) -> ProviderOutput:
    content = message.get("content")
    if request.requirements.required_output == "structured_json":
        if not isinstance(content, str):
            raise ValueError
        value = _load_json(content)
        return ProviderOutput("structured_json", _dump_json(value))
    parts: list[dict[str, object]] = []
    if isinstance(content, str) and content:
        parts.append({"type": "text", "text": content})
    elif content not in {None, ""}:
        raise ValueError
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        parts.extend(
            _neutral_tool_calls(
                tool_calls,
                require_function_type=request.route.adapter != "ollama",
            )
        )
    if not parts:
        raise ValueError
    return ProviderOutput("standard", _dump_json(parts))


def _neutral_tool_calls(
    value: object, *, require_function_type: bool = False
) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAXIMUM_TOOL_CALLS:
        raise ValueError
    result: list[dict[str, object]] = []
    for call in value:
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("id"), str)
            or not 1 <= len(call["id"]) <= 200
            or (require_function_type and call.get("type") != "function")
            or (
                not require_function_type and call.get("type") not in {None, "function"}
            )
        ):
            raise ValueError
        function = call.get("function")
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not 1 <= len(function["name"]) <= 200
        ):
            raise ValueError
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments_json = _dump_json(arguments)
        elif isinstance(arguments, str):
            parsed = _load_json(arguments)
            if not isinstance(parsed, dict):
                raise ValueError
            arguments_json = _dump_json(parsed)
        else:
            raise ValueError
        if len(arguments_json.encode("utf-8")) > _MAXIMUM_TOOL_ARGUMENT_BYTES:
            raise ValueError
        result.append(
            {
                "type": "tool_call",
                "id": call["id"],
                "name": function["name"],
                "arguments_json": arguments_json,
            }
        )
    return result


async def _openai_stream(
    response: httpx.Response, request: ProviderAttemptRequest
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    usage: tuple[UsageAmount, ...] = (UsageAmount("request", Decimal(1)),)
    tool_parts: dict[int, dict[str, str]] = defaultdict(dict)
    finished = False
    done = False
    usage_reported = False
    try:
        async for data in _sse_data(response):
            if done:
                raise ValueError
            if data == b"[DONE]":
                done = True
                continue
            root = _json_object(data)
            if root.get("error") is not None:
                raise _stream_error_failure(root["error"])
            if root.get("usage") is not None:
                if usage_reported:
                    raise ValueError
                usage = _openai_usage(root["usage"])
                usage_reported = True
            choices = root.get("choices")
            if choices == []:
                continue
            if not isinstance(choices, list) or len(choices) != 1 or finished:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ValueError
            refusal = delta.get("refusal")
            if isinstance(refusal, str) and refusal:
                raise _failure("refusal")
            if refusal not in {None, ""}:
                raise ValueError
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield ProviderOutput("text_delta", _dump_json(content))
            elif content not in {None, ""}:
                raise ValueError
            _collect_stream_tools(delta.get("tool_calls"), tool_parts)
            finish_reason = choice.get("finish_reason")
            if finish_reason == "content_filter":
                raise _failure("refusal")
            if finish_reason is not None:
                if finish_reason not in {"stop", "length", "tool_calls"}:
                    raise ValueError
                finished = True
        if not done or not finished:
            raise ValueError
        if sorted(tool_parts) != list(range(len(tool_parts))):
            raise ValueError
    except ProviderFailureError as error:
        raise ProviderFailureError(
            error.failure_class, usage=usage, phase=error.phase
        ) from None
    except httpx.TimeoutException:
        raise ProviderFailureError("timeout", usage=usage) from None
    except httpx.HTTPError:
        raise ProviderFailureError("transport", usage=usage) from None
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise ProviderFailureError("invalid_response", usage=usage) from None
    tool_outputs: list[ProviderOutput] = []
    try:
        if (
            tool_parts
            and "tool_calling" not in request.requirements.required_capabilities
        ):
            raise ValueError
        for index in sorted(tool_parts):
            item = tool_parts[index]
            calls = _neutral_tool_calls(
                [
                    {
                        "id": item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments", ""),
                        },
                    }
                ],
                require_function_type=True,
            )
            tool_outputs.append(ProviderOutput("tool_call", _dump_json(calls[0])))
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise ProviderFailureError("invalid_response", usage=usage) from None
    for output in tool_outputs:
        yield output
    yield ProviderCompleted(usage)


def _collect_stream_tools(value: object, parts: dict[int, dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError
    for call in value:
        if (
            not isinstance(call, dict)
            or type(call.get("index")) is not int
            or not 0 <= call["index"] < _MAXIMUM_TOOL_CALLS
            or (call["index"] not in parts and len(parts) >= _MAXIMUM_TOOL_CALLS)
        ):
            raise ValueError
        item = parts[call["index"]]
        identifier = call.get("id")
        if identifier is not None:
            if (
                not isinstance(identifier, str)
                or not 1 <= len(identifier) <= 200
                or (item.get("id") not in {None, identifier})
            ):
                raise ValueError
            item["id"] = identifier
        function = call.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ValueError
            name = function.get("name")
            if name is not None:
                if (
                    not isinstance(name, str)
                    or not 1 <= len(name) <= 200
                    or item.get("name") not in {None, name}
                ):
                    raise ValueError
                item["name"] = name
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise ValueError
                item["arguments"] = item.get("arguments", "") + arguments
                if (
                    len(item["arguments"].encode("utf-8"))
                    > _MAXIMUM_TOOL_ARGUMENT_BYTES
                ):
                    raise ValueError


def _ollama_completion(
    payload: bytes, request: ProviderAttemptRequest
) -> tuple[ProviderOutput, tuple[UsageAmount, ...]]:
    root = _json_object(payload)
    usage = _ollama_usage(root)
    try:
        if root.get("done") is not True or root.get("error") is not None:
            raise ValueError
        message = root.get("message")
        if not isinstance(message, dict):
            raise ValueError
        normalized = dict(message)
        calls = normalized.get("tool_calls")
        if calls is not None:
            normalized["tool_calls"] = _ollama_tool_calls(calls)
        return _provider_output(normalized, request), usage
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise _failure("invalid_response", usage=usage) from None


def _ollama_tool_calls(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAXIMUM_TOOL_CALLS:
        raise ValueError
    result = []
    for index, call in enumerate(value):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ValueError
        result.append({"id": f"ollama-{index + 1}", "function": call["function"]})
    return result


async def _ollama_stream(
    response: httpx.Response, request: ProviderAttemptRequest
) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
    usage = _request_usage()
    terminal = False
    tool_calls: list[dict[str, object]] = []
    try:
        async for line in _bounded_lines(response):
            if not line:
                continue
            root = _json_object(line)
            if root.get("error") is not None or terminal:
                raise ValueError
            message = root.get("message")
            if not isinstance(message, dict):
                raise ValueError
            content = message.get("content")
            if isinstance(content, str) and content:
                yield ProviderOutput("text_delta", _dump_json(content))
            elif content not in {None, ""}:
                raise ValueError
            calls = message.get("tool_calls")
            if calls:
                normalized_calls = _ollama_tool_calls(calls)
                if len(tool_calls) + len(normalized_calls) > _MAXIMUM_TOOL_CALLS:
                    raise ValueError
                tool_calls.extend(normalized_calls)
            if root.get("done") is True:
                usage = _ollama_usage(root)
                terminal = True
            elif root.get("done") is not False:
                raise ValueError
        if not terminal:
            raise ValueError
    except ProviderFailureError:
        raise
    except httpx.TimeoutException:
        raise ProviderFailureError("timeout", usage=usage) from None
    except httpx.HTTPError:
        raise ProviderFailureError("transport", usage=usage) from None
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise ProviderFailureError("invalid_response", usage=usage) from None
    tool_outputs: list[ProviderOutput] = []
    try:
        if (
            tool_calls
            and "tool_calling" not in request.requirements.required_capabilities
        ):
            raise ValueError
        for index, call in enumerate(tool_calls):
            call["id"] = f"ollama-{index + 1}"
            tool_outputs.append(
                ProviderOutput("tool_call", _dump_json(_neutral_tool_calls([call])[0]))
            )
    except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
        raise ProviderFailureError("invalid_response", usage=usage) from None
    for output in tool_outputs:
        yield output
    yield ProviderCompleted(usage)


def _openai_usage(value: object) -> tuple[UsageAmount, ...]:
    result = [UsageAmount("request", Decimal(1))]
    if value is None:
        return tuple(result)
    if not isinstance(value, dict):
        raise ValueError
    prompt = _nonnegative_integer(value.get("prompt_tokens"))
    completion = _nonnegative_integer(value.get("completion_tokens"))
    details = value.get("prompt_tokens_details")
    cached = 0
    if details is not None:
        if not isinstance(details, dict):
            raise ValueError
        cached = _nonnegative_integer(details.get("cached_tokens", 0))
    if cached > prompt:
        raise ValueError
    result.extend(
        (
            UsageAmount("input_token", Decimal(prompt - cached)),
            UsageAmount("cached_input_token", Decimal(cached)),
            UsageAmount("output_token", Decimal(completion)),
        )
    )
    return tuple(result)


def _ollama_usage(value: Mapping[str, object]) -> tuple[UsageAmount, ...]:
    return (
        UsageAmount("request", Decimal(1)),
        UsageAmount(
            "input_token", Decimal(_nonnegative_integer(value.get("prompt_eval_count")))
        ),
        UsageAmount(
            "output_token", Decimal(_nonnegative_integer(value.get("eval_count")))
        ),
    )


async def _read_response(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAXIMUM_RESPONSE_BYTES:
            raise ValueError
    if not body:
        raise ValueError
    return bytes(body)


async def _bounded_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    pending = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAXIMUM_RESPONSE_BYTES:
            raise ValueError
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > _MAXIMUM_EVENT_BYTES:
                raise ValueError
            yield bytes(line)
        if len(pending) > _MAXIMUM_EVENT_BYTES:
            raise ValueError
    if pending:
        yield bytes(pending)


async def _sse_data(response: httpx.Response) -> AsyncIterator[bytes]:
    data: list[bytes] = []
    async for line in _bounded_lines(response):
        if not line:
            if data:
                value = b"\n".join(data)
                if len(value) > _MAXIMUM_EVENT_BYTES:
                    raise ValueError
                yield value
                data = []
            continue
        if line.startswith(b":"):
            continue
        if line.startswith(b"data:"):
            item = line[5:]
            data.append(item[1:] if item.startswith(b" ") else item)
        elif line.startswith((b"event:", b"id:", b"retry:")):
            continue
        else:
            raise ValueError
    if data:
        value = b"\n".join(data)
        if len(value) > _MAXIMUM_EVENT_BYTES:
            raise ValueError
        yield value


def _validate_headers(response: httpx.Response) -> None:
    raw = response.headers.raw
    if (
        len(raw) > _MAXIMUM_HEADERS
        or sum(len(key) + len(value) for key, value in raw) > _MAXIMUM_HEADER_BYTES
    ):
        raise ValueError
    length = response.headers.get("content-length")
    if length is not None and (
        not length.isascii()
        or not length.isdecimal()
        or int(length) > _MAXIMUM_RESPONSE_BYTES
    ):
        raise ValueError


def _require_content_type(response: httpx.Response, expected: str) -> None:
    _require_one_content_type(response, {expected})


def _require_one_content_type(response: httpx.Response, expected: set[str]) -> None:
    value = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if value not in expected:
        raise ValueError


def _http_failure(status: int) -> ProviderFailureError:
    if status in {401, 403}:
        return _failure("authentication")
    if status == 429:
        return _failure("rate_limited")
    if status in {408, 504}:
        return _failure("timeout")
    if status in {400, 409, 413, 415, 422}:
        return _failure("incompatible")
    if status in {404, 405, 410} or 500 <= status <= 599:
        return _failure("unavailable")
    return _failure("invalid_response")


def _stream_error_failure(value: object) -> ProviderFailureError:
    if not isinstance(value, dict):
        return _failure("invalid_response")
    status = value.get("code")
    if type(status) is int and 100 <= status <= 599:
        return _http_failure(status)
    return _failure("unavailable")


def _bearer_authorization(credential: str) -> str:
    if not 1 <= len(credential.encode("utf-8")) <= 10_000 or any(
        not 0x21 <= ord(character) <= 0x7E for character in credential
    ):
        raise ValueError
    return f"Bearer {credential}"


def _failure(
    failure_class: str,
    *,
    usage: tuple[UsageAmount, ...] = (),
    phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
) -> ProviderFailureError:
    return ProviderFailureError(cast("Any", failure_class), usage=usage, phase=phase)


def _request_usage() -> tuple[UsageAmount, ...]:
    return (UsageAmount("request", Decimal(1)),)


def _failure_with_available_usage(
    error: ProviderFailureError,
) -> ProviderFailureError:
    return _failure(
        error.failure_class,
        usage=error.usage or _request_usage(),
        phase=error.phase,
    )


def _copy_optional(
    source: Mapping[str, object], target: dict[str, object], names: Mapping[str, str]
) -> None:
    for native_name, provider_name in names.items():
        value = source.get(native_name)
        if value is not None:
            target[provider_name] = value


def _dump_request(value: object) -> bytes:
    body = _dump_json(value).encode("utf-8")
    if not 1 <= len(body) <= _MAXIMUM_REQUEST_BYTES:
        raise ValueError
    return body


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _json_object(value: bytes) -> dict[str, object]:
    parsed = _load_json(value.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError
    return cast("dict[str, object]", parsed)


def _load_json(value: str) -> object:
    return json.loads(value, parse_constant=lambda _value: _raise_value_error())


def _raise_value_error() -> None:
    raise ValueError


def _nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value

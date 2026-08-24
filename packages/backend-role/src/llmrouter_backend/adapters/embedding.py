"""Bounded remote embedding adapters for accepted provider protocols."""
# ruff: noqa: C901, D107, EM101, PLR2004, SIM117, TRY004

from __future__ import annotations

import ipaddress
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit

import httpx

from llmrouter_backend.accounting import UsageAmount
from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureClass,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)
from llmrouter_backend.embedding_contract import validate_embedding_inputs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

type OpenAIEmbeddingAdapterName = Literal["openai", "openai_compatible", "custom"]

_OPENAI_ENDPOINT = "https://api.openai.com/v1"
_MAXIMUM_REQUEST_BYTES = 512 * 1024
_MAXIMUM_RESPONSE_BYTES = 5_000_000
_MAXIMUM_HEADERS = 100
_MAXIMUM_HEADER_BYTES = 32 * 1024
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
_REMOTE_USAGE_UNITS = frozenset({"input_token", "request"})


class OpenAIEmbeddingAdapter:
    """Map one native batch to an accepted OpenAI embedding profile."""

    usage_units = _REMOTE_USAGE_UNITS

    def __init__(
        self,
        adapter_name: OpenAIEmbeddingAdapterName,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._adapter_name = adapter_name
        self._transport = transport

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare the complete priced unit set for one embedding batch."""
        return self.usage_units if operation.kind == "embedding" else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Run one complete bounded batch without redirects or environment trust."""
        if (
            request.route.adapter != self._adapter_name
            or request.kind != "embedding"
            or request.streaming
            or request.input_media
        ):
            raise _failure("incompatible")
        if self._adapter_name == "openai" and not request.credential:
            raise _failure("authentication")
        try:
            endpoint = _openai_endpoint(request)
            body = _openai_request(request)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if request.credential is not None:
                headers["Authorization"] = _bearer_authorization(request.credential)
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure("incompatible") from None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint.rstrip('/')}/embeddings",
                    content=body,
                    headers=headers,
                ) as response:
                    try:
                        _validate_headers(response)
                        if response.status_code >= 300:
                            raise _http_failure(response.status_code)
                        _require_json(response)
                        payload = await _read_response(response)
                        output, usage = _openai_response(payload, request)
                        yield output
                        yield ProviderCompleted(usage)
                    except ProviderFailureError as error:
                        raise _failure_with_request(error) from None
                    except httpx.TimeoutException:
                        raise _failure("timeout", with_request=True) from None
                    except httpx.HTTPError:
                        raise _failure("transport", with_request=True) from None
                    except (
                        KeyError,
                        TypeError,
                        UnicodeError,
                        ValueError,
                        RecursionError,
                    ):
                        raise _failure("invalid_response", with_request=True) from None
        except ProviderFailureError:
            raise
        except httpx.TimeoutException:
            raise _failure("timeout") from None
        except httpx.HTTPError:
            raise _failure("transport") from None


class OllamaEmbeddingAdapter:
    """Map one native batch to the bounded Ollama embedding protocol."""

    usage_units = _REMOTE_USAGE_UNITS

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare Ollama request and input-token units for embeddings."""
        return self.usage_units if operation.kind == "embedding" else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Run one native Ollama embedding batch on one connection."""
        if (
            request.route.adapter != "ollama"
            or request.kind != "embedding"
            or request.streaming
            or request.input_media
        ):
            raise _failure("incompatible")
        try:
            endpoint = _route_endpoint(request.route.endpoint)
            body = _ollama_request(request)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if request.credential is not None:
                headers["Authorization"] = _bearer_authorization(request.credential)
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure("incompatible") from None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint.rstrip('/')}/api/embed",
                    content=body,
                    headers=headers,
                ) as response:
                    try:
                        _validate_headers(response)
                        if response.status_code >= 300:
                            raise _http_failure(response.status_code)
                        _require_json(response)
                        payload = await _read_response(response)
                        output, usage = _ollama_response(payload, request)
                        yield output
                        yield ProviderCompleted(usage)
                    except ProviderFailureError as error:
                        raise _failure_with_request(error) from None
                    except httpx.TimeoutException:
                        raise _failure("timeout", with_request=True) from None
                    except httpx.HTTPError:
                        raise _failure("transport", with_request=True) from None
                    except (
                        KeyError,
                        TypeError,
                        UnicodeError,
                        ValueError,
                        RecursionError,
                    ):
                        raise _failure("invalid_response", with_request=True) from None
        except ProviderFailureError:
            raise
        except httpx.TimeoutException:
            raise _failure("timeout") from None
        except httpx.HTTPError:
            raise _failure("transport") from None


def _openai_endpoint(request: ProviderAttemptRequest) -> str:
    if request.route.adapter == "openai":
        if request.route.endpoint is not None:
            raise ValueError
        return _OPENAI_ENDPOINT
    return _route_endpoint(request.route.endpoint)


def _route_endpoint(value: str | None) -> str:
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


def _native_inputs(request: ProviderAttemptRequest) -> list[str]:
    if len(request.request_json.encode("utf-8")) > _MAXIMUM_REQUEST_BYTES:
        raise ValueError
    native = json.loads(request.request_json, parse_constant=_reject_constant)
    if not isinstance(native, dict) or not set(native) <= {
        "workspace_api_name",
        "selector",
        "inputs",
        "tags",
    }:
        raise ValueError
    inputs = native.get("inputs")
    if (
        not isinstance(inputs, list)
        or len(inputs) != request.expected_embedding_count
        or any(not isinstance(value, str) or not value for value in inputs)
    ):
        raise ValueError
    typed = cast("list[str]", inputs)
    validate_embedding_inputs(typed)
    return typed


def _openai_request(request: ProviderAttemptRequest) -> bytes:
    return _dump_request(
        {
            "model": request.route.provider_model_name,
            "input": _native_inputs(request),
            "encoding_format": "float",
        }
    )


def _ollama_request(request: ProviderAttemptRequest) -> bytes:
    return _dump_request(
        {
            "model": request.route.provider_model_name,
            "input": _native_inputs(request),
            "truncate": False,
        }
    )


def _openai_response(
    body: bytes, request: ProviderAttemptRequest
) -> tuple[ProviderOutput, tuple[UsageAmount, ...]]:
    value = _json_object(body)
    data = value.get("data")
    if not isinstance(data, list) or len(data) != request.expected_embedding_count:
        raise ValueError
    indexed: dict[int, list[int | float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError
        index = item.get("index")
        vector = item.get("embedding")
        if type(index) is not int or index in indexed or not isinstance(vector, list):
            raise ValueError
        indexed[index] = cast("list[int | float]", vector)
    count = request.expected_embedding_count
    if type(count) is not int or set(indexed) != set(range(count)):
        raise ValueError
    usage_value = value.get("usage")
    usage = [UsageAmount("request", Decimal(1))]
    if usage_value is not None:
        if not isinstance(usage_value, dict):
            raise ValueError
        usage.append(
            UsageAmount(
                "input_token",
                Decimal(_nonnegative_integer(usage_value.get("prompt_tokens"))),
            )
        )
    vectors = [indexed[index] for index in range(count)]
    return ProviderOutput("embedding", _dump_json(vectors)), tuple(usage)


def _ollama_response(
    body: bytes, request: ProviderAttemptRequest
) -> tuple[ProviderOutput, tuple[UsageAmount, ...]]:
    value = _json_object(body)
    embeddings = value.get("embeddings")
    if (
        not isinstance(embeddings, list)
        or len(embeddings) != request.expected_embedding_count
    ):
        raise ValueError
    usage = (
        UsageAmount("request", Decimal(1)),
        UsageAmount(
            "input_token",
            Decimal(_nonnegative_integer(value.get("prompt_eval_count"))),
        ),
    )
    return ProviderOutput("embedding", _dump_json(embeddings)), usage


def _dump_request(value: object) -> bytes:
    result = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if not 1 <= len(result) <= _MAXIMUM_REQUEST_BYTES:
        raise ValueError
    return result


def _dump_json(value: object) -> str:
    result = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if not 1 <= len(result.encode("utf-8")) <= _MAXIMUM_RESPONSE_BYTES:
        raise ValueError
    return result


def _json_object(value: bytes) -> Mapping[str, object]:
    parsed = json.loads(value, parse_constant=_reject_constant)
    if not isinstance(parsed, dict):
        raise ValueError
    return cast("Mapping[str, object]", parsed)


def _reject_constant(_value: str) -> None:
    raise ValueError


def _nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


async def _read_response(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAXIMUM_RESPONSE_BYTES:
            raise ValueError
    if not body:
        raise ValueError
    return bytes(body)


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


def _require_json(response: httpx.Response) -> None:
    value = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if value != "application/json":
        raise ValueError


def _bearer_authorization(credential: str) -> str:
    if not 1 <= len(credential.encode("utf-8")) <= 10_000 or any(
        not 0x21 <= ord(character) <= 0x7E for character in credential
    ):
        raise ValueError
    return f"Bearer {credential}"


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


def _failure_with_request(error: ProviderFailureError) -> ProviderFailureError:
    return ProviderFailureError(
        error.failure_class,
        usage=(UsageAmount("request", Decimal(1)),),
        phase=error.phase,
    )


def _failure(
    failure_class: ProviderFailureClass,
    *,
    with_request: bool = False,
) -> ProviderFailureError:
    return ProviderFailureError(
        failure_class,
        usage=(UsageAmount("request", Decimal(1)),) if with_request else (),
    )

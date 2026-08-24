"""Bounded WaveSpeed media submission, polling, and result download."""
# ruff: noqa: C901, D107, EM101, PLR0912, PLR0915, PLR2004, TRY003, TRY004, TRY301

from __future__ import annotations

import asyncio
import base64
import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx
from opendle import CallFailurePhase

from llmrouter_backend.accounting import UsageAmount
from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureClass,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from llmrouter_backend.models import UsageUnit

_ENDPOINT = "https://api.wavespeed.ai/api/v3"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0, write=30.0, pool=10.0)
_MAXIMUM_JSON_BYTES = 1024 * 1024
_MAXIMUM_MEDIA_BYTES = 1024 * 1024 * 1024
_MAXIMUM_CREDENTIAL_BYTES = 10_000
_MAXIMUM_RESPONSE_HEADER_BYTES = 64 * 1024
_MAXIMUM_RESPONSE_HEADERS = 128
_READ_CHUNK_BYTES = 64 * 1024
_MODEL_PATH = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,199}){1,7}$"
)
_PREDICTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
    }
)


class WaveSpeedMediaAdapter:
    """Use one fixed WaveSpeed origin without retries or trusted result URLs."""

    usage_units = frozenset({"image", "video_second", "audio_second"})

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or not 0 <= poll_interval_seconds <= 60
        ):
            raise ValueError("The WaveSpeed poll interval is invalid.")
        self._transport = transport
        self._poll_interval_seconds = poll_interval_seconds

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare only units for the selected media kind."""
        if operation.kind != "media":
            return frozenset()
        unit = {
            "image": "image",
            "video": "video_second",
            "audio": "audio_second",
        }.get(operation.requirements.required_output)
        return frozenset({unit}) if unit is not None else frozenset()

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Submit once, poll one accepted task, and download one bounded result."""
        if request.route.adapter != "wavespeed" or request.kind != "media":
            raise _failure("incompatible")
        if request.route.endpoint is not None:
            raise _failure("incompatible")
        if not request.credential:
            raise _failure("authentication")
        try:
            authorization = _authorization(request.credential)
            body = _submission_body(request)
            model = request.route.provider_model_name
            if _MODEL_PATH.fullmatch(model) is None:
                raise ValueError
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure("incompatible") from None
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                status, payload = await _json_request(
                    client,
                    "POST",
                    f"{_ENDPOINT}/{model}",
                    headers=headers,
                    content=json.dumps(body, separators=(",", ":")),
                )
                if status >= 300:
                    raise _http_failure(status, submitted=False)
                if payload is None:
                    raise ValueError
                data = _result_data(payload)
                while data["status"] in {"created", "pending", "processing", "running"}:
                    prediction_id = data.get("id")
                    if (
                        not isinstance(prediction_id, str)
                        or _PREDICTION_ID.fullmatch(prediction_id) is None
                    ):
                        raise _failure(
                            "invalid_response", phase=CallFailurePhase.UNCERTAIN
                        )
                    if self._poll_interval_seconds:
                        await asyncio.sleep(self._poll_interval_seconds)
                    status, payload = await _json_request(
                        client,
                        "GET",
                        f"{_ENDPOINT}/predictions/{prediction_id}/result",
                        headers={
                            "Authorization": authorization,
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                        },
                    )
                    if status >= 300:
                        raise _http_failure(status, submitted=True)
                    if payload is None:
                        raise ValueError
                    data = _result_data(payload)
                if data["status"] != "completed":
                    raise _failure(
                        "refusal" if data["status"] == "failed" else "invalid_response",
                        phase=CallFailurePhase.UNCERTAIN,
                    )
                output_url = _one_output_url(data)
                media_type, media_body = await _download_result(
                    client, output_url, request.requirements.required_output
                )
                yield ProviderOutput(
                    "media",
                    json.dumps(
                        {"media_type": media_type, "size_bytes": len(media_body)},
                        separators=(",", ":"),
                    ),
                    media_body,
                )
                yield ProviderCompleted(_usage(request, data))
        except ProviderFailureError:
            raise
        except httpx.TimeoutException:
            raise _failure("timeout", phase=CallFailurePhase.UNCERTAIN) from None
        except httpx.HTTPError:
            raise _failure("transport", phase=CallFailurePhase.UNCERTAIN) from None
        except KeyError, TypeError, UnicodeError, ValueError, RecursionError:
            raise _failure(
                "invalid_response", phase=CallFailurePhase.UNCERTAIN
            ) from None


def _submission_body(request: ProviderAttemptRequest) -> dict[str, object]:
    value = json.loads(request.request_json)
    if not isinstance(value, dict):
        raise ValueError
    if not set(value) <= {
        "workspace_api_name",
        "selector",
        "kind",
        "prompt",
        "tags",
    }:
        raise ValueError
    prompt = value.get("prompt")
    kind = value.get("kind")
    if (
        not isinstance(prompt, str)
        or not prompt
        or kind != request.requirements.required_output
    ):
        raise ValueError
    images = []
    if tuple(len(media.body) for media in request.input_media) != (
        request.requirements.input_image_sizes
    ):
        raise ValueError
    if request.requirements.required_output == "audio" and request.input_media:
        raise ValueError
    for media in request.input_media:
        if media.role != "input" or media.media_type not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }:
            raise ValueError
        images.append(
            f"data:{media.media_type};base64,{base64.b64encode(media.body).decode('ascii')}"
        )
    body: dict[str, object] = {"prompt": prompt}
    if images:
        body["images"] = images
    return body


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    content: str | None = None,
) -> tuple[int, Mapping[str, object] | None]:
    async with client.stream(method, url, headers=headers, content=content) as response:
        _require_bounded_response_headers(response)
        if response.status_code >= 300:
            return response.status_code, None
        return response.status_code, await _json_response(response)


async def _json_response(response: httpx.Response) -> Mapping[str, object]:
    expected_length = _require_identity_and_bounded_length(
        response, _MAXIMUM_JSON_BYTES
    )
    content_type = _one_content_type(response)
    if content_type != "application/json":
        raise ValueError
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK_BYTES):
        if len(body) + len(chunk) > _MAXIMUM_JSON_BYTES:
            raise ValueError
        body.extend(chunk)
    if not body or (expected_length is not None and len(body) != expected_length):
        raise ValueError
    value = json.loads(
        body, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError())
    )
    if not isinstance(value, dict):
        raise ValueError
    return cast("Mapping[str, object]", value)


def _result_data(payload: Mapping[str, object]) -> dict[str, object]:
    if not set(payload) <= {"code", "message", "data"}:
        raise ValueError
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError
    status = data.get("status")
    if status not in {
        "created",
        "pending",
        "processing",
        "running",
        "completed",
        "failed",
    }:
        raise ValueError
    return cast("dict[str, object]", data)


def _one_output_url(data: Mapping[str, object]) -> str:
    outputs = data.get("outputs")
    if (
        not isinstance(outputs, list)
        or len(outputs) != 1
        or not isinstance(outputs[0], str)
    ):
        raise ValueError
    value = outputs[0]
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or not (host == "wavespeed.ai" or host.endswith(".wavespeed.ai"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or len(value) > 4096
    ):
        raise ValueError
    return value


async def _download_result(
    client: httpx.AsyncClient, url: str, kind: str
) -> tuple[str, bytes]:
    async with client.stream(
        "GET",
        url,
        headers={"Accept": f"{kind}/*", "Accept-Encoding": "identity"},
    ) as response:
        _require_bounded_response_headers(response)
        if response.status_code != 200:
            raise _http_failure(response.status_code, submitted=True)
        expected_length = _require_identity_and_bounded_length(
            response, _MAXIMUM_MEDIA_BYTES
        )
        media_type = _one_content_type(response)
        if media_type not in _MEDIA_TYPES or not media_type.startswith(f"{kind}/"):
            raise _failure("invalid_response", phase=CallFailurePhase.UNCERTAIN)
        result = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK_BYTES):
            if len(result) + len(chunk) > _MAXIMUM_MEDIA_BYTES:
                raise _failure("invalid_response", phase=CallFailurePhase.UNCERTAIN)
            result.extend(chunk)
        if not result or (
            expected_length is not None and len(result) != expected_length
        ):
            raise _failure("invalid_response", phase=CallFailurePhase.UNCERTAIN)
        body = bytes(result)
        if not _media_signature_matches(media_type, body):
            raise _failure("invalid_response", phase=CallFailurePhase.UNCERTAIN)
        return media_type, body


def _usage(
    request: ProviderAttemptRequest, data: Mapping[str, object]
) -> tuple[UsageAmount, ...]:
    output = request.requirements.required_output
    unit = {"image": "image", "video": "video_second", "audio": "audio_second"}[output]
    if output == "image":
        return (UsageAmount(cast("UsageUnit", unit), Decimal(1)),)
    raw_duration = data.get("duration")
    if not isinstance(raw_duration, int | float) or isinstance(raw_duration, bool):
        raise ValueError
    quantity = Decimal(str(raw_duration))
    if not quantity.is_finite() or not 0 < quantity <= 86_400:
        raise ValueError
    return (UsageAmount(cast("UsageUnit", unit), quantity),)


def _authorization(value: str) -> str:
    if not 1 <= len(value.encode("utf-8")) <= _MAXIMUM_CREDENTIAL_BYTES or any(
        not 0x21 <= ord(character) <= 0x7E for character in value
    ):
        raise ValueError
    return f"Bearer {value}"


def _require_bounded_response_headers(response: httpx.Response) -> None:
    items = response.headers.multi_items()
    try:
        header_bytes = sum(
            len(name.encode("latin-1")) + len(value.encode("latin-1")) + 4
            for name, value in items
        )
    except UnicodeError:
        raise ValueError from None
    if (
        len(items) > _MAXIMUM_RESPONSE_HEADERS
        or header_bytes > _MAXIMUM_RESPONSE_HEADER_BYTES
        or len(response.headers.get_list("content-type")) > 1
    ):
        raise ValueError


def _require_identity_and_bounded_length(
    response: httpx.Response, maximum_bytes: int
) -> int | None:
    _require_bounded_response_headers(response)
    encodings = response.headers.get_list("content-encoding")
    if encodings and encodings != ["identity"]:
        raise ValueError
    lengths = response.headers.get_list("content-length")
    if lengths and (
        len(lengths) != 1
        or not lengths[0].isdigit()
        or not 1 <= int(lengths[0]) <= maximum_bytes
    ):
        raise ValueError
    return int(lengths[0]) if lengths else None


def _one_content_type(response: httpx.Response) -> str:
    values = response.headers.get_list("content-type")
    if len(values) != 1:
        raise ValueError
    return values[0].partition(";")[0].strip().lower()


def _http_failure(status: int, *, submitted: bool) -> ProviderFailureError:
    failure = (
        "invalid_response"
        if 300 <= status < 400
        else "authentication"
        if status in {401, 403}
        else "rate_limited"
        if status == 429
        else "incompatible"
        if 400 <= status < 500
        else "unavailable"
    )
    return _failure(
        cast("ProviderFailureClass", failure),
        phase=(
            CallFailurePhase.UNCERTAIN
            if submitted
            else CallFailurePhase.BEFORE_VISIBLE_OUTPUT
        ),
    )


def _failure(
    failure_class: ProviderFailureClass,
    *,
    usage: tuple[UsageAmount, ...] = (),
    phase: CallFailurePhase = CallFailurePhase.BEFORE_VISIBLE_OUTPUT,
) -> ProviderFailureError:
    return ProviderFailureError(failure_class, usage=usage, phase=phase)


def _media_signature_matches(media_type: str, body: bytes) -> bool:
    prefixes = {
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "video/webm": (b"\x1aE\xdf\xa3",),
        "audio/mpeg": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
        "audio/ogg": (b"OggS",),
    }
    if media_type in prefixes:
        return body.startswith(prefixes[media_type])
    if media_type == "image/webp":
        return len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    if media_type in {"video/mp4", "audio/mp4"}:
        return len(body) >= 12 and body[4:8] == b"ftyp"
    if media_type == "audio/wav":
        return len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WAVE"
    return False

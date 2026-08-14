"""Native stream version-one validation and encoding."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from .errors import ExecutionError, ExecutionErrorCode
from .model import ExecutionKind, ExecutionTarget

STREAM_VERSION = "1"
MAXIMUM_EVENT_BYTES = 1024 * 1024
MAXIMUM_DELTA_BYTES = 256 * 1024
KEEPALIVE_SECONDS = 15

_REQUIRED_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "request.admitted": frozenset({"state", "state_revision", "admission"}),
    "request.running": frozenset({"state_revision"}),
    "request.waiting_for_tool": frozenset(
        {"state_revision", "tool_call_id", "expires_at"}
    ),
    "output.delta": frozenset({"output_index", "content_type", "delta"}),
    "output.completed": frozenset({"output_index", "content_type"}),
    "tool.call": frozenset(
        {"tool_call_id", "tool_name", "arguments_delta", "complete"}
    ),
    "tool.started": frozenset({"tool_call_id", "tool_kind"}),
    "tool.completed": frozenset({"tool_call_id", "result_summary"}),
    "tool.failed": frozenset({"tool_call_id", "error", "uncertain_effect"}),
    "usage.updated": frozenset({"usage", "estimated"}),
    "request.cancel_requested": frozenset({"state_revision"}),
    "request.terminal": frozenset(
        {"state", "state_revision", "partial_output", "committed_effects"}
    ),
}
_EXTENSION_NAME = re.compile(r"^extension\.[a-z0-9][a-z0-9._-]{0,99}$")


class EventCompatibility(StrEnum):
    """Client handling for one event name."""

    KNOWN = "known"
    IGNORE_EXTENSION = "ignore_extension"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One exact retained stream event."""

    target: ExecutionTarget
    sequence: int
    event_name: str
    occurred_at: datetime
    payload: Mapping[str, object]
    wire_data: str
    expires_at: datetime | None = None

    def sse(self) -> str:
        """Return one UTF-8 SSE record with one data line."""
        return (
            f"id: {self.sequence}\nevent: {self.event_name}\ndata: {self.wire_data}\n\n"
        )


def event_compatibility(event_name: str) -> EventCompatibility:
    """Reject unknown core events and ignore versioned extensions."""
    if event_name in _REQUIRED_PAYLOAD_FIELDS:
        return EventCompatibility.KNOWN
    if _EXTENSION_NAME.fullmatch(event_name) is not None:
        return EventCompatibility.IGNORE_EXTENSION
    raise ExecutionError(ExecutionErrorCode.STREAM_INCOMPATIBLE, "stream")


def make_event(  # noqa: PLR0913
    target: ExecutionTarget,
    *,
    sequence: int,
    event_name: str,
    occurred_at: datetime,
    payload: dict[str, object],
    expires_at: datetime | None = None,
) -> StreamEvent:
    """Validate and encode one closed version-one envelope."""
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        msg = "A stream sequence must be positive."
        raise ValueError(msg)
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        msg = "A stream event time must include a time zone."
        raise ValueError(msg)
    normalized_occurred_at = _millisecond_time(occurred_at)
    compatibility = event_compatibility(event_name)
    if compatibility is EventCompatibility.KNOWN:
        missing = _REQUIRED_PAYLOAD_FIELDS[event_name] - payload.keys()
        if missing:
            msg = "A stream event payload is incomplete."
            raise ValueError(msg)
        _validate_payload_types(event_name, payload)
    if event_name == "output.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str) or len(delta.encode()) > MAXIMUM_DELTA_BYTES:
            msg = "A stream delta exceeds the byte limit."
            raise ValueError(msg)
    normalized_payload = _json_value(payload)
    if not isinstance(normalized_payload, dict):
        message = "A stream payload must be an object."
        raise TypeError(message)
    envelope: dict[str, object] = {
        "stream_version": STREAM_VERSION,
        "request_id": target.public_id,
        "sequence": sequence,
        "occurred_at": _format_time(normalized_occurred_at),
        "payload": normalized_payload,
    }
    if target.kind is ExecutionKind.AGENT_RUN:
        envelope["run_id"] = target.public_id
    wire_data = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if "\n" in wire_data or len(wire_data.encode()) > MAXIMUM_EVENT_BYTES:
        msg = "A stream event exceeds the byte limit."
        raise ValueError(msg)
    return StreamEvent(
        target,
        sequence,
        event_name,
        normalized_occurred_at,
        _freeze_mapping(normalized_payload),
        wire_data,
        expires_at,
    )


def split_utf8_delta(
    value: str, *, maximum_bytes: int = MAXIMUM_DELTA_BYTES
) -> tuple[str, ...]:
    """Split text without breaking a UTF-8 code point."""
    if maximum_bytes < 1:
        msg = "The delta byte limit must be positive."
        raise ValueError(msg)
    if not value:
        return ("",)
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in value:
        size = len(character.encode())
        if size > maximum_bytes:
            msg = "The delta byte limit cannot contain one character."
            raise ValueError(msg)
        if current and current_bytes + size > maximum_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current:
        chunks.append("".join(current))
    return tuple(chunks)


def keepalive() -> str:
    """Return a comment that has no contract meaning."""
    return ": keepalive\n\n"


def _format_time(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _millisecond_time(value: datetime) -> datetime:
    utc = value.astimezone(UTC)
    return utc.replace(microsecond=(utc.microsecond // 1000) * 1000)


def _validate_payload_types(  # noqa: C901, PLR0912 -- Explicit closed event variants.
    event_name: str, payload: Mapping[str, object]
) -> None:
    revision = payload.get("state_revision")
    if revision is not None and (
        not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
    ):
        msg = "A state revision must be a positive integer."
        raise ValueError(msg)
    if event_name == "request.admitted" and (
        payload.get("state") != "admitted"
        or not isinstance(payload.get("admission"), dict)
    ):
        msg = "An admission event has invalid state or receipt data."
        raise ValueError(msg)
    if event_name == "request.terminal" and (
        payload.get("state")
        not in {"succeeded", "failed", "interrupted", "cancelled", "uncertain"}
        or not isinstance(payload.get("partial_output"), bool)
        or not isinstance(payload.get("committed_effects"), bool)
    ):
        msg = "A terminal event has invalid lifecycle data."
        raise ValueError(msg)
    for name in ("tool_call_id", "tool_name", "content_type", "tool_kind"):
        if name in payload and (
            not isinstance(payload[name], str) or not payload[name]
        ):
            msg = "A stream event text identity is invalid."
            raise ValueError(msg)
    for name in ("output_index",):
        value = payload.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            msg = "A stream output index is invalid."
            raise ValueError(msg)
    for name in ("complete", "estimated", "uncertain_effect"):
        if name in payload and not isinstance(payload[name], bool):
            msg = "A stream event boolean is invalid."
            raise ValueError(msg)
    if event_name == "tool.started" and payload.get("tool_kind") not in {
        "shared",
        "business",
    }:
        msg = "A tool kind is invalid."
        raise ValueError(msg)
    if event_name == "tool.call" and not isinstance(
        payload.get("arguments_delta"), str
    ):
        msg = "A tool arguments delta must be text."
        raise ValueError(msg)
    if event_name == "usage.updated" and not isinstance(payload.get("usage"), dict):
        msg = "A usage update must contain an object."
        raise ValueError(msg)
    if event_name == "tool.failed" and not isinstance(payload.get("error"), dict):
        msg = "A tool failure must contain a safe error object."
        raise ValueError(msg)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    msg = "A stream payload contains a non-JSON value."
    raise TypeError(msg)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            msg = "A stream payload contains a non-string object key."
            raise TypeError(msg)
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        msg = "A stream payload contains a non-finite number."
        raise TypeError(msg)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    msg = "A stream payload contains a non-JSON value."
    raise TypeError(msg)

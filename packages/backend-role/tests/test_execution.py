"""Execution lifecycle and native stream contract tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionKind,
    ExecutionTarget,
    TerminalError,
    event_compatibility,
    keepalive,
    make_event,
    split_utf8_delta,
)

NOW = datetime(2026, 8, 14, 12, 30, 45, 123456, tzinfo=UTC)
TARGET = ExecutionTarget(ExecutionKind.MODEL, "0198a080-0000-7000-8000-000000000001")
RUN = ExecutionTarget(ExecutionKind.AGENT_RUN, "0198a080-0000-7000-8000-000000000002")


def test_event_has_exact_stable_envelope_and_sse() -> None:  # noqa: D103
    event = make_event(
        TARGET,
        sequence=2,
        event_name="output.delta",
        occurred_at=NOW,
        payload={"output_index": 0, "content_type": "text/plain", "delta": "hello"},
    )

    assert json.loads(event.wire_data) == {
        "stream_version": "1",
        "request_id": TARGET.public_id,
        "sequence": 2,
        "occurred_at": "2026-08-14T12:30:45.123Z",
        "payload": {
            "output_index": 0,
            "content_type": "text/plain",
            "delta": "hello",
        },
    }
    assert event.occurred_at == datetime(2026, 8, 14, 12, 30, 45, 123000, tzinfo=UTC)
    assert event.sse() == (f"id: 2\nevent: output.delta\ndata: {event.wire_data}\n\n")
    assert keepalive() == ": keepalive\n\n"


def test_run_envelope_has_matching_request_and_run_ids() -> None:  # noqa: D103
    event = make_event(
        RUN,
        sequence=1,
        event_name="request.admitted",
        occurred_at=NOW,
        payload={"state": "admitted", "state_revision": 1, "admission": {}},
    )

    envelope = json.loads(event.wire_data)
    assert envelope["request_id"] == RUN.public_id
    assert envelope["run_id"] == RUN.public_id


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [
        ("", 4, ("",)),
        ("abcd", 4, ("abcd",)),
        ("abcde", 4, ("abcd", "e")),
        ("a€b", 4, ("a€", "b")),
        ("😀😀", 4, ("😀", "😀")),
    ],
)
def test_delta_split_preserves_utf8_code_points(  # noqa: D103
    value: str, maximum: int, expected: tuple[str, ...]
) -> None:
    chunks = split_utf8_delta(value, maximum_bytes=maximum)

    assert chunks == expected
    assert "".join(chunks) == value
    assert all(len(chunk.encode()) <= maximum for chunk in chunks)


def test_delta_and_event_byte_limits_are_closed() -> None:  # noqa: D103
    payload = {"output_index": 0, "content_type": "text/plain"}
    make_event(
        TARGET,
        sequence=1,
        event_name="output.delta",
        occurred_at=NOW,
        payload={**payload, "delta": "x" * (256 * 1024)},
    )

    with pytest.raises(ValueError, match="delta exceeds"):
        make_event(
            TARGET,
            sequence=1,
            event_name="output.delta",
            occurred_at=NOW,
            payload={**payload, "delta": "x" * (256 * 1024 + 1)},
        )
    with pytest.raises(ValueError, match="event exceeds"):
        make_event(
            TARGET,
            sequence=1,
            event_name="extension.test",
            occurred_at=NOW,
            payload={"data": "x" * (1024 * 1024)},
        )


def test_event_names_accept_only_known_core_or_valid_extensions() -> None:  # noqa: D103
    assert event_compatibility("output.delta").value == "known"
    assert event_compatibility("extension.vendor.v2").value == "ignore_extension"

    for name in ("output.future", "extension.UPPER", "extension."):
        with pytest.raises(ExecutionError) as error:
            event_compatibility(name)
        assert error.value.code is ExecutionErrorCode.STREAM_INCOMPATIBLE


def test_known_event_preserves_unknown_optional_payload_field() -> None:
    """Keep unknown optional data on a known event for forward compatibility."""
    event = make_event(
        TARGET,
        sequence=2,
        event_name="usage.updated",
        occurred_at=NOW,
        payload={"usage": {}, "estimated": True, "future_optional": {"value": 1}},
    )
    assert event.payload["future_optional"] == {"value": 1}


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("request.running", {}),
        ("request.running", {"state_revision": True}),
        (
            "request.terminal",
            {
                "state": "running",
                "state_revision": 2,
                "partial_output": False,
                "committed_effects": False,
            },
        ),
        ("tool.started", {"tool_call_id": "call", "tool_kind": "unknown"}),
    ],
)
def test_known_event_payload_validation_is_closed(  # noqa: D103
    event_name: str, payload: dict[str, object]
) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        make_event(
            TARGET,
            sequence=2,
            event_name=event_name,
            occurred_at=NOW,
            payload=payload,
        )


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        (
            "tool.call",
            {
                "tool_call_id": "call",
                "tool_name": "lookup",
                "arguments_delta": 1,
                "complete": False,
            },
        ),
        ("usage.updated", {"usage": [], "estimated": False}),
        (
            "tool.failed",
            {"tool_call_id": "call", "error": "unsafe", "uncertain_effect": False},
        ),
    ],
)
def test_known_event_rejects_wrong_structured_field_types(
    event_name: str, payload: dict[str, object]
) -> None:
    """Reject required core payload values with the wrong JSON type."""
    with pytest.raises(ValueError):  # noqa: PT011
        make_event(
            TARGET,
            sequence=2,
            event_name=event_name,
            occurred_at=NOW,
            payload=payload,
        )


def test_sequence_and_stop_evidence_reject_boolean_integer_coercion() -> None:
    """Keep JSON booleans separate from integer and evidence fields."""
    with pytest.raises(ValueError, match="sequence"):
        make_event(
            TARGET,
            sequence=True,
            event_name="extension.test",
            occurred_at=NOW,
            payload={},
        )
    with pytest.raises(TypeError, match="booleans"):
        AdapterStopEvidence(
            operation_id="operation",
            supported=True,
            stop_requested=True,
            confirmed_stopped=cast("bool", "true"),
        )


def test_event_payload_is_deeply_frozen_and_detached() -> None:  # noqa: D103
    payload: dict[str, object] = {"nested": {"items": [1, 2]}}
    event = make_event(
        TARGET,
        sequence=2,
        event_name="extension.vendor",
        occurred_at=NOW,
        payload=payload,
    )
    nested = event.payload["nested"]
    assert isinstance(nested, dict) is False

    payload["nested"] = {"items": [9]}
    assert event.payload["nested"] != payload["nested"]
    with pytest.raises(TypeError):
        event.payload["new"] = "value"  # type: ignore[index]


def test_stream_rejects_invalid_time_sequence_and_json_value() -> None:  # noqa: D103
    with pytest.raises(ValueError, match="sequence"):
        make_event(
            TARGET,
            sequence=0,
            event_name="extension.test",
            occurred_at=NOW,
            payload={},
        )
    with pytest.raises(ValueError, match="time zone"):
        make_event(
            TARGET,
            sequence=1,
            event_name="extension.test",
            occurred_at=NOW.replace(tzinfo=None),
            payload={},
        )
    with pytest.raises(TypeError, match="non-JSON"):
        make_event(
            TARGET,
            sequence=1,
            event_name="extension.test",
            occurred_at=NOW,
            payload={"bad": object()},
        )
    with pytest.raises(TypeError, match="non-finite"):
        make_event(
            TARGET,
            sequence=1,
            event_name="extension.test",
            occurred_at=NOW,
            payload={"bad": float("nan")},
        )


@pytest.mark.parametrize(
    "public_id",
    ["not-a-uuid", "00000000-0000-0000-0000-000000000000", TARGET.public_id.upper()],
)
def test_execution_target_requires_canonical_nonzero_uuid(public_id: str) -> None:
    """Reject ambiguous or unsafe public execution identities."""
    with pytest.raises(ValueError, match="UUID|canonical"):  # noqa: RUF043
        ExecutionTarget(ExecutionKind.MODEL, public_id)


def test_execution_target_accepts_uuidv7_with_only_random_a() -> None:
    """Accept the same opaque UUIDv7 random-bit boundary as admission."""
    target = ExecutionTarget(
        ExecutionKind.MODEL, "0198a080-0000-7001-8000-000000000000"
    )
    assert target.public_id.endswith("000000000000")


@pytest.mark.parametrize(
    "document",
    [
        {"class": "timeout", "affected_scope": "attempt", "message": 1},
        {
            "class": "timeout",
            "affected_scope": "attempt",
            "message": "safe",
            "extra": "closed",
        },
        {"class": "future", "affected_scope": "attempt", "message": "safe"},
    ],
)
def test_terminal_error_document_is_strict_and_closed(
    document: dict[str, object],
) -> None:
    """Reject type coercion, extra fields, and unknown error values."""
    with pytest.raises((ValueError, TypeError)):
        TerminalError.from_document(document)

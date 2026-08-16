"""Request fingerprint and UUIDv7 admission value tests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from llmrouter_backend.admission import (
    AdmissionRequest,
    AttachmentReference,
    FingerprintInput,
    RequestKind,
    uuidv7_time,
    validate_uuidv7,
)


def _uuidv7(at: datetime, random_bits: int = 1) -> str:
    milliseconds = int(at.timestamp() * 1000)
    value = (milliseconds << 80) | (7 << 76) | ((random_bits & 0xFFF) << 64)
    value |= 2 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def _fingerprint(**execution: object) -> FingerprintInput:
    fields: dict[str, Any] = {
        "api_version": "1",
        "assignment": "chat",
        "messages": [{"role": "user", "content": "Hello"}],
        "limits": {"logical_timeout_ms": 120000},
        "output": {"format": "text"},
    }
    fields.update(execution)
    return FingerprintInput(
        operation="model.create",
        contract_major=1,
        service_id="service-a",
        workspace_id="workspace-a",
        data_profile="service-data",
        execution=fields,
    )


def test_fingerprint_uses_rfc8785_and_sha256_without_request_identity() -> None:
    """Canonicalize all execution values and keep the UUID outside the digest."""
    value = _fingerprint(
        messages=[
            {
                "role": "user",
                "content": '€$\u000f\nA\'B"\\"/',
                "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
            }
        ]
    )
    canonical = value.canonical_bytes()
    assert b'"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27]' in canonical
    assert value.sha256() == hashlib.sha256(canonical).digest()
    first = AdmissionRequest(
        _uuidv7(datetime(2026, 8, 13, tzinfo=UTC)),
        RequestKind.MODEL,
        value,
        assignment="chat",
    )
    second = AdmissionRequest(
        _uuidv7(datetime(2026, 8, 13, tzinfo=UTC), 2),
        RequestKind.MODEL,
        value,
        assignment="chat",
    )
    assert first.fingerprint.sha256() == second.fingerprint.sha256()


def test_fingerprint_is_immutable_and_rejects_transient_or_unknown_fields() -> None:
    """Accept only the contract-marked execution field set."""
    source = {"role": "user", "content": ["one"]}
    value = _fingerprint(messages=[source])
    source["content"] = ["changed"]
    assert b"changed" not in value.canonical_bytes()
    with pytest.raises(ValueError, match="incomplete or unknown"):
        _fingerprint(trace_context={"traceparent": "secret"})
    with pytest.raises(ValueError, match="incomplete or unknown"):
        _fingerprint(exact_route_grant="secret")
    with pytest.raises(ValueError, match="one request target"):
        _fingerprint(exact_route="route-a")
    with pytest.raises(ValueError, match="does not match authority"):
        FingerprintInput(
            "openai.chat.completions.create",
            1,
            "service-a",
            "workspace-a",
            "service-data",
            {
                "model": "chat",
                "messages": [{"role": "user", "content": "Hello"}],
                "x_llmrouter_workspace_id": "workspace-b",
            },
        )


def test_message_attachments_must_match_the_validated_set() -> None:
    """Bind each message attachment to one validated immutable reference."""
    reference = AttachmentReference("attachment-a", "11" * 32, "application/pdf", 10)
    content = [
        {
            "type": "file",
            "attachment_id": reference.attachment_id,
            "sha256": reference.sha256,
            "media_type": reference.media_type,
        }
    ]
    with pytest.raises(ValueError, match="do not match"):
        _fingerprint(messages=[{"role": "user", "content": content}])
    matched = FingerprintInput(
        "model.create",
        1,
        "service-a",
        "workspace-a",
        "service-data",
        {
            "api_version": "1",
            "assignment": "chat",
            "messages": [{"role": "user", "content": content}],
            "limits": {"logical_timeout_ms": 120000},
            "output": {"format": "text"},
        },
        (reference,),
    )
    assert reference.sha256.encode() in matched.canonical_bytes()
    changed = [dict(content[0], sha256="22" * 32)]
    with pytest.raises(ValueError, match="do not match"):
        FingerprintInput(
            "model.create",
            1,
            "service-a",
            "workspace-a",
            "service-data",
            {
                "api_version": "1",
                "assignment": "chat",
                "messages": [{"role": "user", "content": changed}],
                "limits": {"logical_timeout_ms": 120000},
                "output": {"format": "text"},
            },
            (reference,),
        )
    with pytest.raises(ValueError, match="byte length"):
        AttachmentReference("attachment-b", "33" * 32, "text/plain", 0)


def test_uuidv7_requires_canonical_version_variant_and_extracts_time() -> None:
    """Reject a UUID that is not canonical UUIDv7 with the RFC variant."""
    instant = datetime(2026, 8, 13, 12, 34, 56, 789000, tzinfo=UTC)
    value = _uuidv7(instant)
    assert str(validate_uuidv7(value)) == value
    assert uuidv7_time(value) == instant
    for malformed in (
        str(uuid.uuid4()),
        value.upper(),
        value[:19] + "0" + value[20:],
        value[:14] + "6" + value[15:],
        _uuidv7(instant, 0),
    ):
        with pytest.raises(ValueError, match="UUIDv7"):
            validate_uuidv7(malformed)

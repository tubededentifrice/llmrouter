"""Pure content policy, secret control, manifest, and retention tests."""
# ruff: noqa: E501, PLR2004

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from llmrouter_backend.content import (
    CapturePolicy,
    CaptureReason,
    EffectiveCapture,
    MemoryObjectStore,
    ObjectManifest,
    ObjectSegment,
    RetentionDataClass,
    RetentionLimit,
    RetentionSelection,
    redact_authenticated_values,
    reject_structured_control_fields,
)
from llmrouter_backend.content.errors import ContentError, ContentErrorCode

NOW = datetime(2026, 8, 13, 18, tzinfo=UTC)


def test_structured_control_fields_are_rejected_without_pattern_scanning() -> None:
    """Reject declared control fields but keep secret-like ordinary content."""
    with pytest.raises(ValueError, match="structured control field"):
        reject_structured_control_fields(
            {"messages": [{"text": "safe"}], "authorization-header": "value"}
        )
    document = {
        "messages": [
            {
                "text": "A valid prompt can contain sk-example-shaped text and known-value."
            }
        ]
    }
    reject_structured_control_fields(document)
    reject_structured_control_fields({"message": {"token": "ordinary-content"}})
    assert redact_authenticated_values(document, ("known-value",)) == {
        "messages": [
            {
                "text": "A valid prompt can contain sk-example-shaped text and [REDACTED]."
            }
        ]
    }


def test_capture_snapshot_and_retention_limits_are_closed() -> None:
    """Require one valid immutable expiry and the hard audit safety range."""
    assert EffectiveCapture(
        CapturePolicy.COMPLETE,
        CaptureReason.CONFIGURED,
        "workspace",
        NOW + timedelta(days=7),
    ).expires_at == NOW + timedelta(days=7)
    assert (
        EffectiveCapture(
            CapturePolicy.DISABLED,
            CaptureReason.SPOOL_PRESSURE,
            "workspace",
            None,
        ).expires_at
        is None
    )
    with pytest.raises(ValueError, match="Spool pressure"):
        EffectiveCapture(
            CapturePolicy.COMPLETE,
            CaptureReason.SPOOL_PRESSURE,
            "global",
            NOW + timedelta(days=7),
        )
    limit = RetentionLimit(RetentionDataClass.AGENT_TOOL_AUDIT, 7, 365)
    assert limit.permits(RetentionSelection(RetentionDataClass.AGENT_TOOL_AUDIT, 30))
    with pytest.raises(ValueError, match="7 to 365"):
        RetentionSelection(RetentionDataClass.AGENT_TOOL_AUDIT, 366)
    revisions = RetentionSelection(RetentionDataClass.CONFIGURATION_REVISIONS, 730, 100)
    assert revisions.minimum_count == 100


def test_manifest_and_local_object_store_detect_wrong_or_partial_objects() -> None:
    """Verify immutable puts, wrong bytes, missing bytes, and manifest order."""
    store = MemoryObjectStore()
    value = b"encrypted-segment"
    digest = hashlib.sha256(value).hexdigest()
    store.put("capture/one", value, sha256=digest)
    store.put("capture/one", value, sha256=digest)
    assert store.get("capture/one", sha256=digest) == value
    with pytest.raises(ContentError) as conflict:
        store.put(
            "capture/one", b"changed", sha256=hashlib.sha256(b"changed").hexdigest()
        )
    assert conflict.value.code is ContentErrorCode.CONFLICT
    store.corrupt_for_test("capture/one", b"partial")
    with pytest.raises(ContentError) as integrity:
        store.get("capture/one", sha256=digest)
    assert integrity.value.code is ContentErrorCode.INTEGRITY
    segment = ObjectSegment(1, "capture/one", len(value), digest, b"key", "wrap")
    manifest = ObjectManifest.build("manifest", (segment,))
    assert ObjectManifest.build("manifest", (segment,)).sha256 == manifest.sha256
    with pytest.raises(ValueError, match="contiguous"):
        ObjectManifest.build(
            "bad", (ObjectSegment(2, "capture/two", 1, "0" * 64, b"key", "wrap"),)
        )

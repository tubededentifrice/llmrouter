"""Closed attachment value tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from llmrouter_backend.attachments import (
    ACCEPTED_MEDIA_TYPES,
    MAXIMUM_ATTACHMENT_BYTES,
    AttachmentMetadata,
    AttachmentState,
    CreateAttachment,
)


def _metadata(state: AttachmentState) -> AttachmentMetadata:
    return AttachmentMetadata(
        attachment_id="attachment-a",
        service_id="service-a",
        workspace_id="workspace-a",
        media_type="text/plain",
        byte_length=4,
        sha256="11" * 32,
        state=state,
        expires_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_declaration_accepts_only_fixed_types_lengths_and_lowercase_digest() -> None:
    """Keep the public media and size bounds closed."""
    for media_type in ACCEPTED_MEDIA_TYPES:
        value = CreateAttachment(media_type, 1, "ab" * 32)
        assert value.media_type == media_type
        assert value.sha256 not in repr(value)
    CreateAttachment("application/pdf", MAXIMUM_ATTACHMENT_BYTES, "01" * 32)
    for invalid in (
        ("application/octet-stream", 1, "01" * 32),
        ("text/plain", 0, "01" * 32),
        ("text/plain", MAXIMUM_ATTACHMENT_BYTES + 1, "01" * 32),
        ("text/plain", 1, "AB" * 32),
    ):
        with pytest.raises(ValueError, match="attachment"):
            CreateAttachment(*invalid)


def test_only_ready_metadata_produces_an_admission_reference() -> None:
    """Do not let incomplete or expired content enter request admission."""
    for state in (AttachmentState.AWAITING_CONTENT, AttachmentState.EXPIRED):
        with pytest.raises(ValueError, match="not ready"):
            _metadata(state).require_ready()
    ready = _metadata(AttachmentState.READY)
    ready.require_ready()
    assert ready.sha256 not in repr(ready)

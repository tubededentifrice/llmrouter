"""Closed values for immutable attachment storage."""
# ruff: noqa: D105, EM101, TC003, TRY003

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

MAXIMUM_ATTACHMENT_BYTES = 25 * 1024 * 1024
ACCEPTED_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/json",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "audio/mpeg",
        "audio/wav",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AttachmentState(StrEnum):
    """Public attachment lifecycle states."""

    AWAITING_CONTENT = "awaiting_content"
    READY = "ready"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CreateAttachment:
    """The complete public attachment declaration."""

    media_type: str
    byte_length: int
    sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, str):
            raise TypeError("The attachment media type must be text.")
        if self.media_type not in ACCEPTED_MEDIA_TYPES:
            raise ValueError("The attachment media type is not supported.")
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int):
            raise TypeError("The attachment byte length must be an integer.")
        if not 1 <= self.byte_length <= MAXIMUM_ATTACHMENT_BYTES:
            raise ValueError("The attachment byte length is outside the fixed limit.")
        if not isinstance(self.sha256, str):
            raise TypeError("The attachment digest must be text.")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("The attachment digest must be lowercase SHA-256.")


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    """Safe metadata for one exactly scoped attachment."""

    attachment_id: str
    service_id: str
    workspace_id: str | None
    media_type: str
    byte_length: int
    sha256: str = field(repr=False)
    state: AttachmentState
    expires_at: datetime

    def require_ready(self) -> None:
        """Reject metadata that cannot produce an admission reference."""
        if self.state is not AttachmentState.READY:
            raise ValueError("The attachment is not ready for admission.")


@dataclass(frozen=True, slots=True)
class AttachmentCreateResult:
    """One create receipt and its equality-replay state."""

    value: AttachmentMetadata
    replayed: bool


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    """Decrypted bytes and their response metadata."""

    value: bytes = field(repr=False)
    media_type: str
    sha256: str = field(repr=False)

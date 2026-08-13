"""Scoped immutable attachment storage."""

from .errors import AttachmentError, AttachmentErrorCode
from .model import (
    ACCEPTED_MEDIA_TYPES,
    MAXIMUM_ATTACHMENT_BYTES,
    AttachmentContent,
    AttachmentCreateResult,
    AttachmentMetadata,
    AttachmentState,
    CreateAttachment,
)
from .repository import PostgresAttachmentRepository

__all__ = [
    "ACCEPTED_MEDIA_TYPES",
    "MAXIMUM_ATTACHMENT_BYTES",
    "AttachmentContent",
    "AttachmentCreateResult",
    "AttachmentError",
    "AttachmentErrorCode",
    "AttachmentMetadata",
    "AttachmentState",
    "CreateAttachment",
    "PostgresAttachmentRepository",
]

"""Safe failures for scoped immutable attachments."""
# ruff: noqa: D107, E501

from __future__ import annotations

from enum import StrEnum


class AttachmentErrorCode(StrEnum):
    """Public error classes for attachment operations."""

    INVALID = "attachment_invalid"
    NOT_FOUND = "attachment_not_found"
    ALREADY_COMPLETE = "attachment_already_complete"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    INTERNAL = "internal_error"


class AttachmentError(RuntimeError):
    """One content-free attachment failure."""

    __slots__ = ("code", "request_id")

    def __init__(self, code: AttachmentErrorCode, request_id: str) -> None:
        messages = {
            AttachmentErrorCode.INVALID: "The attachment is invalid.",
            AttachmentErrorCode.NOT_FOUND: "The attachment was not found.",
            AttachmentErrorCode.ALREADY_COMPLETE: "The attachment is already complete.",
            AttachmentErrorCode.INSUFFICIENT_SCOPE: "The request scope is not permitted.",
            AttachmentErrorCode.WORKSPACE_UNAVAILABLE: "The selected workspace is not available.",
            AttachmentErrorCode.INTERNAL: "The attachment content is not available.",
        }
        super().__init__(messages[code])
        self.code = code
        self.request_id = request_id

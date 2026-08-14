"""Safe request admission failures."""
# ruff: noqa: D107, E501

from __future__ import annotations

from enum import StrEnum


class AdmissionErrorCode(StrEnum):
    """Public error codes for request admission and status reads."""

    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    REQUEST_IDENTITY_CONFLICT = "request_identity_conflict"
    REQUEST_IDENTITY_EXPIRED = "request_identity_expired"
    REQUEST_NOT_FOUND = "request_not_found"
    ATTACHMENT_INVALID = "attachment_invalid"
    ASSIGNMENT_UNAVAILABLE = "assignment_unavailable"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    DIAGNOSTIC_PERMISSION_REQUIRED = "diagnostic_permission_required"


class AdmissionError(RuntimeError):
    """One non-disclosing admission failure."""

    __slots__ = ("code", "request_id")

    def __init__(self, code: AdmissionErrorCode, request_id: str) -> None:
        messages = {
            AdmissionErrorCode.INVALID_REQUEST: "The request is invalid.",
            AdmissionErrorCode.INSUFFICIENT_SCOPE: "The request scope is not permitted.",
            AdmissionErrorCode.REQUEST_IDENTITY_CONFLICT: (
                "The request identity was used for different content."
            ),
            AdmissionErrorCode.REQUEST_IDENTITY_EXPIRED: (
                "The request identity cannot be admitted."
            ),
            AdmissionErrorCode.REQUEST_NOT_FOUND: "The request was not found.",
            AdmissionErrorCode.ATTACHMENT_INVALID: (
                "An attachment is not valid for this request."
            ),
            AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE: (
                "The selected assignment is not available."
            ),
            AdmissionErrorCode.WORKSPACE_UNAVAILABLE: (
                "The selected workspace is not available."
            ),
            AdmissionErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED: (
                "The exact route requires a valid diagnostic grant."
            ),
        }
        super().__init__(messages[code])
        self.code = code
        self.request_id = request_id

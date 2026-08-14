"""Safe failures for capture, retention, export, and lifecycle work."""
# ruff: noqa: D107

from enum import StrEnum


class ContentErrorCode(StrEnum):
    """Closed error codes that do not contain sensitive content."""

    INVALID = "invalid_request"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    RECENT_AUTH_REQUIRED = "recent_authentication_required"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTEGRITY = "integrity_failure"
    EXPIRED = "expired"
    STALE_LEASE = "stale_lease"


class ContentError(Exception):
    """One safe content lifecycle failure."""

    def __init__(self, code: ContentErrorCode, request_id: str) -> None:
        super().__init__(code.value)
        self.code = code
        self.request_id = request_id

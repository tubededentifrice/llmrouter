"""Safe public errors from the shared authority boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple, TypedDict


class SafeErrorCode(StrEnum):
    """Authority and mutation errors from the public error catalog."""

    INVALID_TOKEN = "invalid_token"  # noqa: S105  # nosec B105
    RECENT_AUTH_REQUIRED = "recent_auth_required"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    SERVICE_SCOPE_MISMATCH = "service_scope_mismatch"
    WORKSPACE_SCOPE_MISMATCH = "workspace_scope_mismatch"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STATE_REVISION_CONFLICT = "state_revision_conflict"


class SafeErrorBody(TypedDict):
    """The closed safe error body."""

    code: str
    message: str
    retryable: bool
    request_id: str


class SafeErrorEnvelope(TypedDict):
    """The closed safe public error envelope."""

    error: SafeErrorBody


class SafeAuthorityError(RuntimeError):
    """An authority failure that contains no private record data."""

    __slots__ = ("code", "request_id", "status_code")

    def __init__(self, code: SafeErrorCode, request_id: str) -> None:
        """Store only the approved safe error fields."""
        if not request_id:
            msg = "The request identity must not be empty."
            raise ValueError(msg)
        metadata = _SAFE_ERRORS[code]
        super().__init__(metadata.message)
        self.code = code
        self.request_id = request_id
        self.status_code = metadata.status_code

    def to_envelope(self) -> SafeErrorEnvelope:
        """Return the formal closed error envelope."""
        return {
            "error": {
                "code": self.code.value,
                "message": str(self),
                "retryable": False,
                "request_id": self.request_id,
            }
        }


def invalid_token(request_id: str) -> SafeAuthorityError:
    """Create one generic authentication failure."""
    return SafeAuthorityError(SafeErrorCode.INVALID_TOKEN, request_id)


def recent_auth_required(request_id: str) -> SafeAuthorityError:
    """Create one recent-authentication failure."""
    return SafeAuthorityError(SafeErrorCode.RECENT_AUTH_REQUIRED, request_id)


def insufficient_scope(request_id: str) -> SafeAuthorityError:
    """Create one exact-operation denial."""
    return SafeAuthorityError(SafeErrorCode.INSUFFICIENT_SCOPE, request_id)


def hidden_not_found(request_id: str) -> SafeAuthorityError:
    """Hide whether an out-of-scope record exists."""
    return SafeAuthorityError(SafeErrorCode.NOT_FOUND, request_id)


def service_scope_mismatch(request_id: str) -> SafeAuthorityError:
    """Create one explicit service-scope mismatch."""
    return SafeAuthorityError(SafeErrorCode.SERVICE_SCOPE_MISMATCH, request_id)


def workspace_scope_mismatch(request_id: str) -> SafeAuthorityError:
    """Create one explicit workspace-scope mismatch."""
    return SafeAuthorityError(SafeErrorCode.WORKSPACE_SCOPE_MISMATCH, request_id)


def idempotency_conflict(request_id: str) -> SafeAuthorityError:
    """Create one changed idempotency-key replay error."""
    return SafeAuthorityError(SafeErrorCode.IDEMPOTENCY_CONFLICT, request_id)


def revision_conflict(request_id: str) -> SafeAuthorityError:
    """Create one stale expected-revision error."""
    return SafeAuthorityError(SafeErrorCode.STATE_REVISION_CONFLICT, request_id)


class _SafeErrorMetadata(NamedTuple):
    message: str
    status_code: int


_SAFE_ERRORS: dict[SafeErrorCode, _SafeErrorMetadata] = {
    SafeErrorCode.INVALID_TOKEN: _SafeErrorMetadata("Authentication failed.", 401),
    SafeErrorCode.RECENT_AUTH_REQUIRED: _SafeErrorMetadata(
        "Recent authentication is required.", 401
    ),
    SafeErrorCode.INSUFFICIENT_SCOPE: _SafeErrorMetadata(
        "The authenticated actor does not have permission for this operation.", 403
    ),
    SafeErrorCode.NOT_FOUND: _SafeErrorMetadata(
        "The requested record was not found.", 404
    ),
    SafeErrorCode.SERVICE_SCOPE_MISMATCH: _SafeErrorMetadata(
        "The service scope does not match the authenticated authority.", 403
    ),
    SafeErrorCode.WORKSPACE_SCOPE_MISMATCH: _SafeErrorMetadata(
        "The workspace scope does not match the authenticated authority.", 403
    ),
    SafeErrorCode.IDEMPOTENCY_CONFLICT: _SafeErrorMetadata(
        "The idempotency key was used for different content.", 409
    ),
    SafeErrorCode.STATE_REVISION_CONFLICT: _SafeErrorMetadata(
        "The expected revision does not match the current revision.", 409
    ),
}

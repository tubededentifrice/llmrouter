"""Safe provider-routing failures."""

from __future__ import annotations

from enum import StrEnum


class RoutingErrorCode(StrEnum):
    """Stable routing failure codes."""

    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "request_not_found"
    BUSY = "temporarily_unavailable"
    NO_CANDIDATE = "assignment_unavailable"
    LOGICAL_DEADLINE = "logical_deadline"
    CLAIM_CONFLICT = "internal_error"
    DIAGNOSTIC_PERMISSION_REQUIRED = "diagnostic_permission_required"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    INVALID_ADAPTER_RESULT = "internal_error"


class RoutingError(RuntimeError):
    """One safe routing error without provider or request content."""

    def __init__(self, code: RoutingErrorCode, request_id: str) -> None:
        """Set one stable code and safe request identity."""
        self.code = code
        self.request_id = request_id
        public_code = (
            RoutingErrorCode.NO_CANDIDATE.value
            if code is RoutingErrorCode.LOGICAL_DEADLINE
            else code.value
        )
        super().__init__(public_code)

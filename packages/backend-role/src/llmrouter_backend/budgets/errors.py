"""Safe budget policy failures."""

from __future__ import annotations

from enum import StrEnum


class BudgetErrorCode(StrEnum):
    """Closed safe budget error codes."""

    NOT_FOUND = "not_found"
    CONFLICT = "state_revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CURRENCY_MISMATCH = "currency_mismatch"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_REQUIRED = "budget_required"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    INVALID_REQUEST = "invalid_request"
    STALE_ALLOWANCE = "stale_allowance"
    ALLOWANCE_EXPIRED = "allowance_expired"
    ALLOWANCE_EXHAUSTED = "allowance_exhausted"


class BudgetError(Exception):
    """One safe budget failure without financial or secret detail."""

    def __init__(
        self,
        code: BudgetErrorCode,
        request_id: str,
        *,
        scope_kind: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        """Keep only a safe code, request identity, scope, and candidate."""
        self.code = code
        self.request_id = request_id
        self.scope_kind = scope_kind
        self.candidate_id = candidate_id
        super().__init__(code.value)

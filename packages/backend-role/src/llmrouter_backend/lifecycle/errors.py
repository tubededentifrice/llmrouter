"""Safe service and workspace lifecycle failures."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llmrouter_backend.lifecycle.model import LifecycleState


class LifecycleErrorCode(StrEnum):
    """The closed lifecycle error codes."""

    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "not_found"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STATE_REVISION_CONFLICT = "state_revision_conflict"
    TERMINAL_STATE = "terminal_state"
    WORKSPACE_RETIRED = "workspace_retired"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"


class LifecycleError(RuntimeError):
    """A lifecycle failure with only safe current-state detail."""

    __slots__ = ("code", "current_revision", "current_state", "request_id")

    def __init__(
        self,
        code: LifecycleErrorCode,
        request_id: str,
        *,
        current_state: LifecycleState | None = None,
        current_revision: str | None = None,
    ) -> None:
        """Store one closed error and optional safe conflict state."""
        if not request_id:
            msg = "The request identity must not be empty."
            raise ValueError(msg)
        super().__init__(_MESSAGES[code])
        self.code = code
        self.request_id = request_id
        self.current_state = current_state
        self.current_revision = current_revision


_MESSAGES: dict[LifecycleErrorCode, str] = {
    LifecycleErrorCode.INVALID_REQUEST: "The lifecycle request is invalid.",
    LifecycleErrorCode.INSUFFICIENT_SCOPE: (
        "The authenticated actor cannot use this lifecycle operation."
    ),
    LifecycleErrorCode.NOT_FOUND: "The requested record was not found.",
    LifecycleErrorCode.WORKSPACE_NOT_FOUND: (
        "The workspace is not available in this service scope."
    ),
    LifecycleErrorCode.IDEMPOTENCY_CONFLICT: (
        "The idempotency identity was used for different content."
    ),
    LifecycleErrorCode.STATE_REVISION_CONFLICT: (
        "The expected revision does not match the current revision."
    ),
    LifecycleErrorCode.TERMINAL_STATE: "The retired record cannot change.",
    LifecycleErrorCode.WORKSPACE_RETIRED: "The workspace identity is retired.",
    LifecycleErrorCode.WORKSPACE_UNAVAILABLE: (
        "The service tree does not permit new workspace work."
    ),
}

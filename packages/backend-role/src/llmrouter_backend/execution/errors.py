"""Safe execution lifecycle failures."""

from __future__ import annotations

from enum import StrEnum


class ExecutionErrorCode(StrEnum):
    """Stable execution failure codes."""

    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "request_not_found"
    REVISION_CONFLICT = "revision_conflict"
    INVALID_TRANSITION = "invalid_state_transition"
    STREAM_CONFLICT = "stream_event_conflict"
    STREAM_REPLAY_UNAVAILABLE = "stream_replay_unavailable"
    STREAM_INCOMPATIBLE = "stream_incompatible"
    OWNER_FENCED = "owner_fenced"


class ExecutionError(RuntimeError):
    """One safe execution error without request content."""

    def __init__(self, code: ExecutionErrorCode, request_id: str) -> None:
        """Set one stable safe code and request identity."""
        self.code = code
        self.request_id = request_id
        super().__init__(code.value)

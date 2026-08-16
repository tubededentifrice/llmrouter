"""Safe failures for configuration operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConfigurationErrorCode(StrEnum):
    """Public configuration failure codes."""

    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "configuration_revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VALIDATION_FAILED = "invalid_request"
    TERMINAL_STATE = "terminal_state"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One secret-safe configuration validation result."""

    field_path: str
    reason: str


class ConfigurationError(Exception):
    """One configuration failure with safe public details."""

    def __init__(
        self,
        code: ConfigurationErrorCode,
        request_id: str,
        *,
        issues: tuple[ValidationIssue, ...] = (),
        current_revision: str | None = None,
    ) -> None:
        """Store only safe response values."""
        super().__init__(code.value)
        self.code = code
        self.request_id = request_id
        self.issues = issues
        self.current_revision = current_revision

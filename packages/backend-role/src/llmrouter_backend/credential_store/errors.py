"""Secret-safe failures for encrypted credential operations."""

from __future__ import annotations

from enum import StrEnum


class CredentialStoreErrorCode(StrEnum):
    """Public failure codes that do not expose secret material."""

    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STATE_REVISION_CONFLICT = "state_revision_conflict"
    TERMINAL_STATE = "terminal_state"
    CREDENTIAL_UNAVAILABLE = "temporarily_unavailable"


class CredentialStoreError(Exception):
    """One generic credential-store failure with a request identity."""

    def __init__(
        self,
        code: CredentialStoreErrorCode,
        request_id: str,
        *,
        current_revision: str | None = None,
    ) -> None:
        """Store only safe response fields."""
        super().__init__(code.value)
        self.code = code
        self.request_id = request_id
        self.current_revision = current_revision

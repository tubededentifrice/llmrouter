"""Safe failures for administrator authentication and grants."""

from __future__ import annotations


class AdministratorAuthError(RuntimeError):
    """One closed failure without identity or secret details."""

    __slots__ = ("code", "request_id")

    def __init__(self, code: str, request_id: str) -> None:
        """Store only an approved code and the request identity."""
        if code not in {
            "invalid_request",
            "invalid_token",
            "insufficient_scope",
            "recent_auth_required",
            "temporarily_unavailable",
            "not_found",
            "state_revision_conflict",
            "idempotency_conflict",
        }:
            msg = "The administrator authentication error code is not approved."
            raise ValueError(msg)
        if not request_id:
            msg = "The request identity must not be empty."
            raise ValueError(msg)
        super().__init__(_MESSAGES[code])
        self.code = code
        self.request_id = request_id


_MESSAGES = {
    "invalid_request": "The request is not valid.",
    "invalid_token": "Authentication failed.",  # nosec B105
    "insufficient_scope": "The administrator grant does not permit this operation.",
    "recent_auth_required": "Recent authentication is required.",
    "temporarily_unavailable": "The identity service is not available.",
    "not_found": "The requested record was not found.",
    "state_revision_conflict": "The expected revision does not match.",
    "idempotency_conflict": "The idempotency key was used for different content.",
}

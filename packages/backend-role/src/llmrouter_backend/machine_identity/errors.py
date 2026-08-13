"""Generic safe failures for machine credential operations."""

from __future__ import annotations


class MachineIdentityError(RuntimeError):
    """A closed failure that cannot expose a secret or hidden claim."""

    __slots__ = ("code", "request_id")

    def __init__(self, code: str, request_id: str) -> None:
        """Store only one approved code and request identity."""
        if code not in {"invalid_token", "insufficient_scope", "not_found"}:
            msg = "The machine identity error code is not approved."
            raise ValueError(msg)
        if not request_id:
            msg = "The request identity must not be empty."
            raise ValueError(msg)
        super().__init__(_MESSAGES[code])
        self.code = code
        self.request_id = request_id


_MESSAGES = {
    "invalid_token": "Authentication failed.",  # nosec B105
    "insufficient_scope": "The token does not permit this operation.",
    "not_found": "The requested record was not found.",
}

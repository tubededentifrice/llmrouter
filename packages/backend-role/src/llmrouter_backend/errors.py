"""Safe native API failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ApiError(Exception):
    """One stable public failure with no private control data."""

    status_code: int
    code: str
    message: str
    field: str | None = None
    reason: str | None = None

    def envelope(self) -> dict[str, object]:
        """Build the closed public error envelope."""
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.field is not None or self.reason is not None:
            details: dict[str, str] = {}
            if self.field is not None:
                details["field"] = self.field
            if self.reason is not None:
                details["reason"] = self.reason
            error["details"] = details
        return {"error": error}


def authentication_required() -> ApiError:
    """Create the stable missing-or-invalid-authentication result."""
    return ApiError(401, "authentication_required", "Valid authentication is required.")


def not_found(resource: str) -> ApiError:
    """Hide absent and out-of-scope resources behind one result."""
    return ApiError(404, "not_found", f"The {resource} does not exist.")


def conflict(message: str) -> ApiError:
    """Create a safe current-state conflict."""
    return ApiError(409, "conflict", message)


def invalid_request(field: str | None = None, reason: str | None = None) -> ApiError:
    """Create a safe contract or field failure."""
    return ApiError(
        400,
        "invalid_request",
        "The request is invalid.",
        field=field,
        reason=reason,
    )


def assignment_cycle() -> ApiError:
    """Create the stable assignment inheritance cycle result."""
    return ApiError(
        409,
        "assignment_cycle",
        "The assignment inheritance would contain a cycle.",
    )


def provider_unavailable() -> ApiError:
    """Report that no current provider-model can accept the call."""
    return ApiError(
        503,
        "provider_unavailable",
        "No eligible provider-model is available.",
    )


def content_unavailable() -> ApiError:
    """Report safe early loss or expiry of retained media bytes."""
    return ApiError(
        404,
        "content_unavailable",
        "The retained content is not available.",
    )

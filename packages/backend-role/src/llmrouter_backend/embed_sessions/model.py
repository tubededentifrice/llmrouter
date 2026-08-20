"""Closed values for secure service administration embed sessions."""
# ruff: noqa: EM101, TC003, TRY003

from __future__ import annotations

import ipaddress
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from llmrouter_backend.authority import EmbedPrincipal

MAXIMUM_BODY_BYTES = 16_384
MAXIMUM_HEADER_CHARACTERS = 2_048
MAXIMUM_ORIGIN_CHARACTERS = 2_000
SESSION_COOKIE = "__Host-llmrouter-embed"

OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=200)]
HostSubject = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Origin = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
Permission = Literal[
    "configuration.read",
    "configuration.write",
    "budget.read",
    "budget.write",
    "accounting.read",
    "request_status.read",
    "health.read",
    "diagnostic.run",
]

SENSITIVE_PERMISSIONS = frozenset(
    {"configuration.write", "budget.write", "diagnostic.run"}
)
SERVICE_PERMISSIONS = frozenset(
    {"configuration.read", "configuration.write", "health.read"}
)


class ClosedModel(BaseModel):
    """Reject unknown public fields and changes after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EmbedTheme(ClosedModel):
    """One validated set of host-selected theme tokens."""

    mode: Literal["light", "dark", "system"]
    density: Literal["comfortable", "compact"]
    corner_style: Literal["square", "rounded"]


class EmbedSessionRequest(ClosedModel):
    """One host-backend request for bounded frame authority."""

    host_user_subject: HostSubject
    workspace_id: OpaqueId | None = None
    allowed_origin: Origin
    permissions: Annotated[list[Permission], Field(min_length=1, max_length=8)]
    recent_auth_at: datetime | None = None
    theme: EmbedTheme

    @field_validator("allowed_origin")
    @classmethod
    def validate_allowed_origin(cls, value: str) -> str:
        """Require one canonical HTTPS or loopback development origin."""
        return exact_web_origin(value)

    @field_validator("permissions")
    @classmethod
    def validate_unique_permissions(cls, value: list[Permission]) -> list[Permission]:
        """Reject repeated permissions instead of silently reducing them."""
        if len(value) != len(set(value)):
            raise ValueError("Embed permissions must be unique.")
        return value

    @model_validator(mode="after")
    def validate_scope_and_authentication(self) -> EmbedSessionRequest:
        """Keep service-only and sensitive authority closed."""
        permissions = frozenset(self.permissions)
        if self.workspace_id is None and not permissions <= SERVICE_PERMISSIONS:
            raise ValueError("The permission needs an exact workspace.")
        if self.recent_auth_at is not None and (
            self.recent_auth_at.tzinfo is None
            or self.recent_auth_at.utcoffset() is None
        ):
            raise ValueError("The recent-authentication time needs a time zone.")
        return self


class BootstrapRequest(ClosedModel):
    """One same-origin, one-use frame bootstrap redemption."""

    bootstrap_token: Annotated[SecretStr, Field(min_length=43, max_length=200)]
    frame_nonce: Annotated[str, StringConstraints(min_length=16, max_length=200)]
    host_origin: Origin

    @field_validator("host_origin")
    @classmethod
    def validate_host_origin(cls, value: str) -> str:
        """Require the exact canonical host origin from the handshake."""
        return exact_web_origin(value)

    def __repr__(self) -> str:
        """Keep the bootstrap secret out of diagnostics."""
        return (
            "BootstrapRequest(bootstrap_token=[REDACTED], "
            f"frame_nonce={self.frame_nonce!r}, host_origin={self.host_origin!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class CreatedSession:
    """One show-once bootstrap result."""

    session_id: str
    bootstrap_token: str
    frame_url: str
    expires_at: datetime

    def __repr__(self) -> str:
        """Keep the show-once secret out of diagnostics."""
        return (
            f"CreatedSession(session_id={self.session_id!r}, "
            "bootstrap_token=[REDACTED], "
            f"frame_url={self.frame_url!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class RedeemedSession:
    """One principal and browser-hidden session secret after bootstrap."""

    principal: EmbedPrincipal
    session_token: str
    theme: EmbedTheme
    cookie_max_age: int

    def __repr__(self) -> str:
        """Keep the browser session secret out of diagnostics."""
        return (
            f"RedeemedSession(principal={self.principal!r}, "
            "session_token=[REDACTED], "
            f"theme={self.theme!r}, cookie_max_age={self.cookie_max_age!r})"
        )


class EmbedSessionError(RuntimeError):
    """One safe public embed-session failure."""

    __slots__ = ("code", "request_id", "status_code")

    def __init__(self, code: str, request_id: str) -> None:
        """Store only an approved code and request identity."""
        status = {
            "invalid_request": 400,
            "invalid_token": 401,
            "recent_auth_required": 401,
            "insufficient_scope": 403,
            "not_found": 404,
            "temporarily_unavailable": 503,
        }
        if code not in status or not request_id:
            raise ValueError("The embed-session error is invalid.")
        super().__init__(
            {
                "invalid_request": "The request is invalid.",
                "invalid_token": "Authentication failed.",
                "recent_auth_required": "Recent authentication is required.",
                "insufficient_scope": "The token does not permit this operation.",
                "not_found": "The requested record was not found.",
                "temporarily_unavailable": "The Router is temporarily unavailable.",
            }[code]
        )
        self.code = code
        self.request_id = request_id
        self.status_code = status[code]


def exact_web_origin(value: str) -> str:
    """Return one exact canonical web origin or reject it."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("The web origin is invalid.") from error
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("The value must be an exact web origin.")
    hostname = parsed.hostname
    loopback = hostname == "localhost"
    with suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    if parsed.scheme != "https" and not loopback:
        raise ValueError("A non-loopback web origin must use HTTPS.")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    canonical = f"{parsed.scheme}://{rendered_host}"
    if port is not None:
        canonical += f":{port}"
    if value != canonical:
        raise ValueError("The web origin must use its canonical form.")
    return value

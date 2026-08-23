"""Validated runtime configuration for the LLM Router application."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_DEFAULT_ISSUER = "https://auth.opendle.dev"
_DEFAULT_REDIRECT_URI = "https://llmrouter.opendle.dev/v1/admin/oidc/callback"
_OIDC_CALLBACK_PATH = "/v1/admin/oidc/callback"
_DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:5174",
    "https://llmrouter.opendle.dev",
)
_MINIMUM_SESSION_HOURS = 1
_MAXIMUM_SESSION_HOURS = 30 * 24
_MAXIMUM_PORT = 65_535
_MAXIMUM_URL_LENGTH = 4_096
_ASCII_SPACE = 0x20
_ASCII_DELETE = 0x7F


@dataclass(frozen=True, slots=True)
class Settings:
    """Application settings whose control values stay outside request data."""

    public_admin_auth: bool = False
    oidc_issuer: str = _DEFAULT_ISSUER
    oidc_redirect_uri: str = _DEFAULT_REDIRECT_URI
    oidc_client_id_file: Path | None = None
    oidc_client_secret_file: Path | None = None
    administrator_subjects_file: Path | None = None
    administrator_digest_key_file: Path | None = None
    administrator_encryption_key_file: Path | None = None
    administrator_session_hours: int = 24
    allowed_origins: tuple[str, ...] = _DEFAULT_ALLOWED_ORIGINS

    def __post_init__(self) -> None:
        """Reject unsafe session and origin configuration."""
        if (
            not _MINIMUM_SESSION_HOURS
            <= self.administrator_session_hours
            <= (_MAXIMUM_SESSION_HOURS)
        ):
            message = "Administrator session expiry must be from 1 hour to 30 days."
            raise ValueError(message)
        if not _valid_http_url(self.oidc_issuer, expected_path=""):
            message = "The OpenID Connect issuer must be one exact HTTP authority."
            raise ValueError(message)
        if not _valid_http_url(
            self.oidc_redirect_uri, expected_path=_OIDC_CALLBACK_PATH
        ):
            message = "The OpenID Connect redirect must use the exact callback path."
            raise ValueError(message)
        if not self.allowed_origins or any(
            not _valid_origin(origin) for origin in self.allowed_origins
        ):
            message = "Each administrator origin must be one exact HTTP origin."
            raise ValueError(message)
        if len(set(self.allowed_origins)) != len(self.allowed_origins):
            message = "Administrator origins must be unique."
            raise ValueError(message)

    @classmethod
    def from_environment(cls) -> Settings:
        """Load paths and non-secret deployment controls from the environment."""
        public_auth = os.environ.get("LLMROUTER_PUBLIC_ADMIN_AUTH", "0")
        if public_auth not in {"0", "1"}:
            message = "LLMROUTER_PUBLIC_ADMIN_AUTH must be 0 or 1."
            raise ValueError(message)
        raw_hours = os.environ.get("LLMROUTER_ADMIN_SESSION_HOURS", "24")
        try:
            hours = int(raw_hours)
        except ValueError:
            message = "LLMROUTER_ADMIN_SESSION_HOURS must be an integer."
            raise ValueError(message) from None
        raw_origins = os.environ.get(
            "LLMROUTER_ADMIN_ALLOWED_ORIGINS", ",".join(_DEFAULT_ALLOWED_ORIGINS)
        )
        origins = tuple(raw_origins.split(","))
        return cls(
            public_admin_auth=public_auth == "1",
            oidc_issuer=os.environ.get("LLMROUTER_OIDC_ISSUER", _DEFAULT_ISSUER),
            oidc_redirect_uri=os.environ.get(
                "LLMROUTER_OIDC_REDIRECT_URI", _DEFAULT_REDIRECT_URI
            ),
            oidc_client_id_file=_path("LLMROUTER_OIDC_CLIENT_ID_FILE"),
            oidc_client_secret_file=_path("LLMROUTER_OIDC_CLIENT_SECRET_FILE"),
            administrator_subjects_file=_path("LLMROUTER_ADMINISTRATOR_SUBJECTS_FILE"),
            administrator_digest_key_file=_path("LLMROUTER_ADMIN_DIGEST_KEY_FILE"),
            administrator_encryption_key_file=_path(
                "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE"
            ),
            administrator_session_hours=hours,
            allowed_origins=origins,
        )


def _path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _valid_origin(origin: str) -> bool:
    return _valid_http_url(origin, expected_path="")


def _valid_http_url(value: str, *, expected_path: str) -> bool:
    parsed = urlsplit(value)
    try:
        parsed_port = parsed.port
    except ValueError:
        return False
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in ({"http", "https"} if loopback else {"https"}):
        return False
    return bool(
        parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path == expected_path
        and not parsed.query
        and not parsed.fragment
        and "?" not in value
        and "#" not in value
        and "\\" not in value
        and (parsed_port is None or 1 <= parsed_port <= _MAXIMUM_PORT)
        and value.strip() == value
        and not any(
            ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE
            for character in value
        )
        and len(value) <= _MAXIMUM_URL_LENGTH
    )

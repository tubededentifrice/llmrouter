"""Validated runtime configuration for the LLM Router application."""

from __future__ import annotations

import os
import re
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
_DEFAULT_OBJECT_STORE_REGION = "us-east-1"
_MINIMUM_SESSION_HOURS = 1
_MAXIMUM_SESSION_HOURS = 30 * 24
_MAXIMUM_PORT = 65_535
_MAXIMUM_URL_LENGTH = 4_096
_ASCII_SPACE = 0x20
_ASCII_DELETE = 0x7F
_MINIMUM_BUCKET_LENGTH = 3
_MAXIMUM_BUCKET_LENGTH = 63
_MAXIMUM_OBJECT_STORE_CONNECT_TIMEOUT = 30
_MAXIMUM_OBJECT_STORE_READ_TIMEOUT = 120
_MAXIMUM_LOCAL_EMBEDDING_THREADS = 32
_MAXIMUM_MEDIA_JOB_DEADLINE_SECONDS = 24 * 60 * 60
_MAXIMUM_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 10 * 60
_MAXIMUM_CALL_CONNECTION_TIMEOUT_SECONDS = 15 * 60
_MAXIMUM_CONCURRENCY = 100_000
_MAXIMUM_REQUEST_BODY_BYTES = 1024 * 1024 * 1024
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IP_BUCKET_NAME = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")
_OBJECT_STORE_REGION = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    provider_credential_wrapping_key_file: Path | None = None
    administrator_session_hours: int = 24
    allowed_origins: tuple[str, ...] = _DEFAULT_ALLOWED_ORIGINS
    object_store_endpoint: str | None = None
    object_store_bucket: str | None = None
    object_store_region: str = _DEFAULT_OBJECT_STORE_REGION
    object_store_access_key_file: Path | None = None
    object_store_secret_key_file: Path | None = None
    object_store_ca_file: Path | None = None
    object_store_connect_timeout_seconds: int = 2
    object_store_read_timeout_seconds: int = 10
    local_embedding_cache_dir: Path | None = None
    local_embedding_artifact_sha256: str | None = None
    local_embedding_threads: int = 1
    provider_attempt_timeout_seconds: int = 60
    call_connection_timeout_seconds: int = 15 * 60
    call_concurrency: int = 100
    database_concurrency: int = 50
    maximum_request_body_bytes: int = 70 * 1024 * 1024
    media_job_deadline_seconds: int = 60 * 60

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
        if self.provider_credential_wrapping_key_file is not None and (
            self.provider_credential_wrapping_key_file
            in {
                self.administrator_digest_key_file,
                self.administrator_encryption_key_file,
                self.object_store_secret_key_file,
            }
        ):
            message = "The provider credential wrapping key must have one purpose."
            raise ValueError(message)
        _validate_object_store(self)
        _validate_local_embedding(self)
        _validate_call_limits(self)
        if (
            type(self.maximum_request_body_bytes) is not int
            or not 1 <= self.maximum_request_body_bytes <= _MAXIMUM_REQUEST_BODY_BYTES
        ):
            message = "The request-body limit must be from 1 byte through 1 GiB."
            raise ValueError(message)
        if (
            type(self.media_job_deadline_seconds) is not int
            or not 1
            <= self.media_job_deadline_seconds
            <= _MAXIMUM_MEDIA_JOB_DEADLINE_SECONDS
        ):
            message = "The media-job deadline must be from 1 second through 24 hours."
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
            provider_credential_wrapping_key_file=_path(
                "LLMROUTER_PROVIDER_CREDENTIAL_WRAPPING_KEY_FILE"
            ),
            administrator_session_hours=hours,
            allowed_origins=origins,
            object_store_endpoint=os.environ.get("LLMROUTER_OBJECT_STORE_ENDPOINT"),
            object_store_bucket=os.environ.get("LLMROUTER_OBJECT_STORE_BUCKET"),
            object_store_region=os.environ.get(
                "LLMROUTER_OBJECT_STORE_REGION", _DEFAULT_OBJECT_STORE_REGION
            ),
            object_store_access_key_file=_path(
                "LLMROUTER_OBJECT_STORE_ACCESS_KEY_FILE"
            ),
            object_store_secret_key_file=_path(
                "LLMROUTER_OBJECT_STORE_SECRET_KEY_FILE"
            ),
            object_store_ca_file=_path("LLMROUTER_OBJECT_STORE_CA_FILE"),
            object_store_connect_timeout_seconds=_integer_environment(
                "LLMROUTER_OBJECT_STORE_CONNECT_TIMEOUT_SECONDS", 2
            ),
            object_store_read_timeout_seconds=_integer_environment(
                "LLMROUTER_OBJECT_STORE_READ_TIMEOUT_SECONDS", 10
            ),
            local_embedding_cache_dir=_path("LLMROUTER_LOCAL_EMBEDDING_CACHE_DIR"),
            local_embedding_artifact_sha256=os.environ.get(
                "LLMROUTER_LOCAL_EMBEDDING_ARTIFACT_SHA256"
            ),
            local_embedding_threads=_integer_environment(
                "LLMROUTER_LOCAL_EMBEDDING_THREADS", 1
            ),
            provider_attempt_timeout_seconds=_integer_environment(
                "LLMROUTER_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 60
            ),
            call_connection_timeout_seconds=_integer_environment(
                "LLMROUTER_CALL_CONNECTION_TIMEOUT_SECONDS", 15 * 60
            ),
            call_concurrency=_integer_environment("LLMROUTER_CALL_CONCURRENCY", 100),
            database_concurrency=_integer_environment(
                "LLMROUTER_DATABASE_CONCURRENCY", 50
            ),
            maximum_request_body_bytes=_integer_environment(
                "LLMROUTER_MAXIMUM_REQUEST_BODY_BYTES", 70 * 1024 * 1024
            ),
            media_job_deadline_seconds=_integer_environment(
                "LLMROUTER_MEDIA_JOB_DEADLINE_SECONDS", 60 * 60
            ),
        )


def _path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _integer_environment(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        message = f"{name} must be an integer."
        raise ValueError(message) from None


def _validate_object_store(settings: Settings) -> None:
    values = (
        settings.object_store_endpoint,
        settings.object_store_bucket,
        settings.object_store_access_key_file,
        settings.object_store_secret_key_file,
    )
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        message = "The object-store configuration must be complete."
        raise ValueError(message)
    if settings.object_store_endpoint is not None and not _valid_object_store_url(
        settings.object_store_endpoint
    ):
        message = "The object-store endpoint is not safe."
        raise ValueError(message)
    if settings.object_store_bucket is not None and not (
        _MINIMUM_BUCKET_LENGTH
        <= len(settings.object_store_bucket)
        <= _MAXIMUM_BUCKET_LENGTH
        and all(
            character.isascii()
            and (character.islower() or character.isdigit() or character in ".-")
            for character in settings.object_store_bucket
        )
        and _BUCKET_NAME.fullmatch(settings.object_store_bucket) is not None
        and ".." not in settings.object_store_bucket
        and ".-" not in settings.object_store_bucket
        and "-." not in settings.object_store_bucket
        and _IP_BUCKET_NAME.fullmatch(settings.object_store_bucket) is None
    ):
        message = "The object-store bucket is not valid."
        raise ValueError(message)
    if _OBJECT_STORE_REGION.fullmatch(settings.object_store_region) is None:
        message = "The object-store region is not valid."
        raise ValueError(message)
    if not (
        1
        <= settings.object_store_connect_timeout_seconds
        <= _MAXIMUM_OBJECT_STORE_CONNECT_TIMEOUT
    ):
        message = "The object-store connect timeout must be from 1 to 30 seconds."
        raise ValueError(message)
    if not (
        1
        <= settings.object_store_read_timeout_seconds
        <= _MAXIMUM_OBJECT_STORE_READ_TIMEOUT
    ):
        message = "The object-store read timeout must be from 1 to 120 seconds."
        raise ValueError(message)


def _validate_local_embedding(settings: Settings) -> None:
    configured = (
        settings.local_embedding_cache_dir,
        settings.local_embedding_artifact_sha256,
    )
    if any(value is not None for value in configured) and not all(
        value is not None for value in configured
    ):
        message = "The local embedding artifact configuration must be complete."
        raise ValueError(message)
    if settings.local_embedding_cache_dir is not None and not (
        settings.local_embedding_cache_dir.is_absolute()
        and settings.local_embedding_cache_dir.name
    ):
        message = "The local embedding cache path must be absolute."
        raise ValueError(message)
    if (
        settings.local_embedding_artifact_sha256 is not None
        and _SHA256.fullmatch(settings.local_embedding_artifact_sha256) is None
    ):
        message = "The local embedding artifact digest must be SHA-256."
        raise ValueError(message)
    if not 1 <= settings.local_embedding_threads <= _MAXIMUM_LOCAL_EMBEDDING_THREADS:
        message = "The local embedding thread limit must be from 1 through 32."
        raise ValueError(message)


def _validate_call_limits(settings: Settings) -> None:
    if not (
        type(settings.provider_attempt_timeout_seconds) is int
        and 1
        <= settings.provider_attempt_timeout_seconds
        <= _MAXIMUM_PROVIDER_ATTEMPT_TIMEOUT_SECONDS
    ):
        message = "The provider-attempt timeout must be from 1 to 600 seconds."
        raise ValueError(message)
    if not (
        type(settings.call_connection_timeout_seconds) is int
        and 1
        <= settings.call_connection_timeout_seconds
        <= _MAXIMUM_CALL_CONNECTION_TIMEOUT_SECONDS
    ):
        message = "The call connection timeout must be from 1 to 900 seconds."
        raise ValueError(message)
    for name, value in (
        ("Call", settings.call_concurrency),
        ("Database", settings.database_concurrency),
    ):
        if type(value) is not int or not 1 <= value <= _MAXIMUM_CONCURRENCY:
            message = f"{name} concurrency must be from 1 through 100000."
            raise ValueError(message)


def _valid_origin(origin: str) -> bool:
    return _valid_http_url(origin, expected_path="")


def _valid_object_store_url(value: str) -> bool:
    """Permit standard HTTPS or an explicit loopback development endpoint."""
    return _valid_http_url(value, expected_path="")


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

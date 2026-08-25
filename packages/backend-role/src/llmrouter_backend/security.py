"""Direct service key and administrator OIDC security controls."""
# ruff: noqa: D107, PLR2004, TC001, TC006

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from opendle import (
    OidcClient as SharedOidcClient,
)
from opendle import (
    OidcClientAuthenticationMethod,
    OidcError,
    OidcProtocolError,
    OidcResponseLimitError,
    OidcTransportResponse,
    validate_canonical_token,
)
from opendle import (
    OidcMetadata as SharedOidcMetadata,
)

from llmrouter_backend.config import Settings
from llmrouter_backend.errors import ApiError, authentication_required

if TYPE_CHECKING:
    from collections.abc import Mapping

_CONTROL_ASSOCIATED_DATA = b"llmrouter-administrator-control-v1"
_TOKEN_BYTES = 32
_OIDC_TIMEOUT_SECONDS = 10.0
_MAXIMUM_OIDC_DOCUMENT_BYTES = 1_000_000
_MAXIMUM_OIDC_RESPONSE_HEADERS = 100
_RETURN_PATH = re.compile(r"^/(?:[A-Za-z0-9_?&=.-][A-Za-z0-9/_?&=.-]*)?$")


@dataclass(frozen=True, slots=True)
class ControlKeys:
    """Keys used only to verify or encrypt server-side control values."""

    digest_key: bytes
    encryption_key: bytes

    @classmethod
    def load(cls, settings: Settings) -> ControlKeys:
        """Load the deployment control keys without reading identity values."""
        required_paths = (
            settings.administrator_digest_key_file,
            settings.administrator_encryption_key_file,
        )
        if any(path is None for path in required_paths):
            raise authentication_required()
        digest_source = _read_control_bytes(cast(Path, required_paths[0]))
        encryption_source = _read_control_bytes(cast(Path, required_paths[1]))
        return cls(
            digest_key=hashlib.sha256(digest_source).digest(),
            encryption_key=hashlib.sha256(encryption_source).digest(),
        )

    def verifier(self, value: str) -> bytes:
        """Create a keyed verifier for one high-entropy control token."""
        return hmac.digest(self.digest_key, value.encode("utf-8"), "sha256")

    def encrypt(self, values: Mapping[str, str]) -> bytes:
        """Encrypt short-lived control values for server-side storage."""
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(values, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        return nonce + AESGCM(self.encryption_key).encrypt(
            nonce, plaintext, _CONTROL_ASSOCIATED_DATA
        )

    def decrypt(self, value: bytes) -> dict[str, str]:
        """Decrypt one server-side control object or fail closed."""
        try:
            plaintext = AESGCM(self.encryption_key).decrypt(
                value[:12], value[12:], _CONTROL_ASSOCIATED_DATA
            )
            document = json.loads(plaintext)
        except (InvalidTag, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise authentication_required() from error
        if not isinstance(document, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in document.items()
        ):
            raise authentication_required()
        return cast(dict[str, str], document)


@dataclass(frozen=True, slots=True)
class AdministratorSecrets:
    """OIDC values loaded without placing them in application data."""

    client_id: str
    client_secret: str
    allowed_subjects: frozenset[str]
    control_keys: ControlKeys

    @classmethod
    def load(cls, settings: Settings) -> AdministratorSecrets:
        """Load and validate all required administrator control files."""
        if not settings.public_admin_auth:
            raise authentication_required()
        required_paths = (
            settings.oidc_client_id_file,
            settings.oidc_client_secret_file,
            settings.administrator_subjects_file,
        )
        if any(path is None for path in required_paths):
            raise authentication_required()
        client_id = _read_text(cast(Path, required_paths[0]), 2_000)
        client_secret = _read_text(cast(Path, required_paths[1]), 4_000)
        subjects_text = _read_text(cast(Path, required_paths[2]), 100_000)
        allowed_subjects = frozenset(
            line.strip() for line in subjects_text.splitlines() if line.strip()
        )
        if not allowed_subjects or any(len(item) > 500 for item in allowed_subjects):
            raise authentication_required()
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            allowed_subjects=allowed_subjects,
            control_keys=ControlKeys.load(settings),
        )

    def verifier(self, value: str) -> bytes:
        """Create a keyed verifier with the deployment digest key."""
        return self.control_keys.verifier(value)

    def encrypt(self, values: Mapping[str, str]) -> bytes:
        """Encrypt an OIDC or session control object."""
        return self.control_keys.encrypt(values)

    def decrypt(self, value: bytes) -> dict[str, str]:
        """Decrypt an OIDC or session control object."""
        return self.control_keys.decrypt(value)


@dataclass(frozen=True, slots=True)
class OidcMetadata:
    """Exact trusted endpoints from the configured issuer."""

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    _shared: SharedOidcMetadata = field(repr=False, compare=False)

    def trusted_shared(self) -> SharedOidcMetadata:
        """Return the bound metadata when the public endpoint view is unchanged."""
        if (
            self.authorization_endpoint != self._shared.authorization_endpoint
            or self.token_endpoint != self._shared.token_endpoint
            or self.jwks_uri != self._shared.jwks_uri
        ):
            raise authentication_required()
        return self._shared


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """Verified immutable OIDC authority and display data."""

    issuer: str
    subject: str
    display_name: str


class _HttpxOidcTransport:
    """Adapt the Router HTTPX transport to the shared OIDC client."""

    def __init__(self, transport: httpx.BaseTransport | None) -> None:
        self._transport = transport

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> OidcTransportResponse:
        """Read one bounded response without redirects or environment trust."""
        with (
            httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
                trust_env=False,
            ) as client,
            client.stream(method, url, headers=headers, content=body) as response,
        ):
            if len(response.headers.multi_items()) > _MAXIMUM_OIDC_RESPONSE_HEADERS:
                message = "The OpenID Connect response has too many headers."
                raise OidcResponseLimitError(message)
            lengths = response.headers.get_list("content-length")
            if len(lengths) > 1 or (
                lengths
                and (
                    not lengths[0].isascii()
                    or not lengths[0].isdecimal()
                    or int(lengths[0]) > _MAXIMUM_OIDC_DOCUMENT_BYTES
                )
            ):
                message = "The OpenID Connect response length is invalid."
                raise OidcProtocolError(message)
            if (
                len(response.headers.get_list("content-type")) > 1
                or len(response.headers.get_list("content-encoding")) > 1
            ):
                message = "The OpenID Connect response headers are invalid."
                raise OidcProtocolError(message)
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > _MAXIMUM_OIDC_DOCUMENT_BYTES:
                    message = (
                        "The OpenID Connect response exceeds the document byte bound."
                    )
                    raise OidcResponseLimitError(message)
                content.extend(chunk)
            return OidcTransportResponse(
                status=response.status_code,
                headers=dict(response.headers.items()),
                body=bytes(content),
            )


class OidcClient:
    """Complete OIDC authorization-code and PKCE client."""

    def __init__(
        self,
        settings: Settings,
        secrets_: AdministratorSecrets,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets_
        try:
            self._client = SharedOidcClient(
                issuer=settings.oidc_issuer,
                client_id=secrets_.client_id,
                client_secret=secrets_.client_secret,
                redirect_uri=settings.oidc_redirect_uri,
                token_authentication_method=(
                    OidcClientAuthenticationMethod.CLIENT_SECRET_BASIC
                ),
                timeout=_OIDC_TIMEOUT_SECONDS,
                maximum_document_bytes=_MAXIMUM_OIDC_DOCUMENT_BYTES,
                transport=_HttpxOidcTransport(transport),
            )
        except TypeError, ValueError:
            raise authentication_required() from None

    def metadata(self) -> OidcMetadata:
        """Read and validate the configured issuer discovery document."""
        try:
            metadata = self._client.discover()
        except OidcError, TypeError, ValueError:
            raise authentication_required() from None
        return OidcMetadata(
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            metadata.jwks_uri,
            metadata,
        )

    def authorization_url(
        self, metadata: OidcMetadata, *, state: str, nonce: str, verifier: str
    ) -> str:
        """Build the exact authorization code request with S256 PKCE."""
        try:
            return self._client.authorization_url(
                self._shared_metadata(metadata),
                state=state,
                nonce=nonce,
                code_verifier=verifier,
                include_offline_access=True,
            )
        except OidcError, TypeError, ValueError:
            raise authentication_required() from None

    def exchange(self, metadata: OidcMetadata, *, code: str, verifier: str) -> str:
        """Exchange one authorization code using the exact redirect and verifier."""
        try:
            return self._client.exchange_code(
                self._shared_metadata(metadata),
                code=code,
                code_verifier=verifier,
            )
        except OidcError, TypeError, ValueError:
            raise authentication_required() from None

    def verify_identity(
        self, metadata: OidcMetadata, *, id_token: str, nonce: str
    ) -> OidcIdentity:
        """Verify the ID token signature, authority, audience, expiry, and nonce."""
        try:
            verified = self._client.verify_id_token(
                self._shared_metadata(metadata),
                id_token=id_token,
                expected_nonce=nonce,
            )
        except OidcError, TypeError, ValueError:
            raise authentication_required() from None
        subject = verified.subject
        if (
            not 1 <= len(subject) <= 500
            or subject not in self._secrets.allowed_subjects
        ):
            raise ApiError(403, "permission_denied", "Administrator access is denied.")
        display = verified.claims.get("name") or verified.claims.get(
            "preferred_username"
        )
        if (
            not isinstance(display, str)
            or not 1 <= len(display) <= 200
            or "\x00" in display
        ):
            display = "Pocket ID administrator"
        return OidcIdentity(
            issuer=verified.issuer,
            subject=subject,
            display_name=display,
        )

    def _shared_metadata(self, metadata: OidcMetadata) -> SharedOidcMetadata:
        return metadata.trusted_shared()


def new_token() -> str:
    """Create one canonical token with 256 random bits."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def require_canonical_token(value: str) -> str:
    """Fail closed unless one supplied control token has the generated shape."""
    try:
        return validate_canonical_token(value)
    except ValueError as error:
        raise authentication_required() from error


def create_service_key(key_id: str) -> str:
    """Create a self-locating direct bearer key with 256 random bits."""
    return f"llmr_sk_{key_id}_{new_token()}"


def service_key_id(value: str) -> str | None:
    """Read the public key record identifier from a direct bearer key."""
    prefix = "llmr_sk_"
    if not value.startswith(prefix):
        return None
    identifier, separator, random_value = value[len(prefix) :].partition("_")
    if not separator or len(random_value) != 43:
        return None
    try:
        parsed = uuid.UUID(identifier)
    except ValueError:
        return None
    return str(parsed)


def valid_return_path(value: str) -> bool:
    """Accept one local absolute path without a network-path reference."""
    return bool(1 <= len(value) <= 1_000 and _RETURN_PATH.fullmatch(value))


def _read_text(path: Path, maximum: int) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise authentication_required() from error
    if not 1 <= len(value) <= maximum or "\x00" in value:
        raise authentication_required()
    return value


def _read_control_bytes(path: Path) -> bytes:
    try:
        value = path.read_bytes().strip()
    except OSError as error:
        raise authentication_required() from error
    if len(value) < 32:
        raise authentication_required()
    return value

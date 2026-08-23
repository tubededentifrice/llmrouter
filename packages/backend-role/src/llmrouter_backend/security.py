"""Direct service key and administrator OIDC security controls."""
# ruff: noqa: D107, PLR2004, TC001, TC006

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeIs, cast
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from opendle import build_authorization_code_url, validate_canonical_token

from llmrouter_backend.config import Settings
from llmrouter_backend.errors import ApiError, authentication_required

if TYPE_CHECKING:
    from collections.abc import Mapping

_CONTROL_ASSOCIATED_DATA = b"llmrouter-administrator-control-v1"
_TOKEN_BYTES = 32
_OIDC_TIMEOUT_SECONDS = 10.0
_MAXIMUM_OIDC_DOCUMENT_BYTES = 1_000_000
_MAXIMUM_NUMERIC_DATE = 253_402_300_799
_RETURN_PATH = re.compile(r"^/(?:[A-Za-z0-9_?&=.-][A-Za-z0-9/_?&=.-]*)?$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


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


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """Verified immutable OIDC authority and display data."""

    issuer: str
    subject: str
    display_name: str


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
        self._transport = transport

    def metadata(self) -> OidcMetadata:
        """Read and validate the configured issuer discovery document."""
        issuer = self._settings.oidc_issuer
        _validate_exact_issuer(issuer)
        document = self._json_request(
            "GET", f"{issuer}/.well-known/openid-configuration"
        )
        if document.get("issuer") != issuer:
            raise authentication_required()
        authorization_endpoint = _required_url(document, "authorization_endpoint")
        token_endpoint = _required_url(document, "token_endpoint")
        jwks_uri = _required_url(document, "jwks_uri")
        if urlsplit(issuer).scheme == "https" and any(
            urlsplit(endpoint).scheme != "https"
            for endpoint in (authorization_endpoint, token_endpoint, jwks_uri)
        ):
            raise authentication_required()
        _require_basic_token_authentication(document)
        return OidcMetadata(authorization_endpoint, token_endpoint, jwks_uri)

    def authorization_url(
        self, metadata: OidcMetadata, *, state: str, nonce: str, verifier: str
    ) -> str:
        """Build the exact authorization code request with S256 PKCE."""
        try:
            return build_authorization_code_url(
                authorization_endpoint=metadata.authorization_endpoint,
                client_id=self._secrets.client_id,
                redirect_uri=self._settings.oidc_redirect_uri,
                state=state,
                nonce=nonce,
                code_verifier=verifier,
            )
        except ValueError as error:
            raise authentication_required() from error

    def exchange(self, metadata: OidcMetadata, *, code: str, verifier: str) -> str:
        """Exchange one authorization code using the exact redirect and verifier."""
        document = self._json_request(
            "POST",
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.oidc_redirect_uri,
                "code_verifier": verifier,
            },
            auth=httpx.BasicAuth(
                username=self._secrets.client_id,
                password=self._secrets.client_secret,
            ),
        )
        token = document.get("id_token")
        if not isinstance(token, str) or not 1 <= len(token) <= 20_000:
            raise authentication_required()
        return token

    def verify_identity(
        self, metadata: OidcMetadata, *, id_token: str, nonce: str
    ) -> OidcIdentity:
        """Verify the ID token signature, authority, audience, expiry, and nonce."""
        header, claims, signing_input, signature = _jwt_parts(id_token)
        key_id = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(key_id, str)
            or not 1 <= len(key_id) <= 500
            or any(
                ord(character) <= 0x20 or ord(character) == 0x7F for character in key_id
            )
        ):
            raise authentication_required()
        jwks = self._json_request("GET", metadata.jwks_uri)
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise authentication_required()
        matches = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == key_id
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if len(matches) != 1:
            raise authentication_required()
        _verify_rs256(cast(dict[str, Any], matches[0]), signing_input, signature)
        issuer = claims.get("iss")
        subject = claims.get("sub")
        if (
            issuer != self._settings.oidc_issuer
            or not isinstance(subject, str)
            or "\x00" in subject
        ):
            raise authentication_required()
        _verify_audience(claims, self._secrets.client_id)
        _verify_time_and_nonce(claims, nonce)
        if (
            not 1 <= len(subject) <= 500
            or subject not in self._secrets.allowed_subjects
        ):
            raise ApiError(403, "permission_denied", "Administrator access is denied.")
        display = claims.get("name") or claims.get("preferred_username")
        if (
            not isinstance(display, str)
            or not 1 <= len(display) <= 200
            or "\x00" in display
        ):
            display = "Pocket ID administrator"
        return OidcIdentity(issuer=issuer, subject=subject, display_name=display)

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
    ) -> dict[str, Any]:
        _validate_endpoint(url)
        try:
            with httpx.Client(
                timeout=_OIDC_TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=self._transport,
                trust_env=False,
            ) as client:
                with client.stream(method, url, data=data, auth=auth) as response:
                    response.raise_for_status()
                    declared_length = response.headers.get("Content-Length")
                    if declared_length is not None and (
                        not declared_length.isascii()
                        or not declared_length.isdecimal()
                        or int(declared_length) > _MAXIMUM_OIDC_DOCUMENT_BYTES
                    ):
                        raise authentication_required()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > _MAXIMUM_OIDC_DOCUMENT_BYTES:
                            raise authentication_required()
                        content.extend(chunk)
                document = json.loads(
                    content,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
        except (httpx.HTTPError, UnicodeError, ValueError) as error:
            raise authentication_required() from error
        if not isinstance(document, dict):
            raise authentication_required()
        return cast(dict[str, Any], document)


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


def _validate_exact_issuer(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.path or parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise authentication_required()
    _validate_endpoint(value)


def _validate_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise authentication_required() from error
    if (
        parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "#" in value
        or "\\" in value
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
        or not 1 <= len(value) <= 4_096
        or value.strip() != value
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise authentication_required()


def _required_url(document: Mapping[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise authentication_required()
    _validate_endpoint(value)
    return value


def _require_basic_token_authentication(document: Mapping[str, Any]) -> None:
    methods = document.get("token_endpoint_auth_methods_supported")
    if methods is None:
        return
    if (
        not isinstance(methods, list)
        or not methods
        or len(methods) > 20
        or any(
            not isinstance(method, str)
            or not 1 <= len(method) <= 100
            or method.strip() != method
            for method in methods
        )
        or len(set(methods)) != len(methods)
        or "client_secret_basic" not in methods
    ):
        raise authentication_required()


def _b64url(value: str, *, maximum: int) -> bytes:
    if not value or len(value) > maximum or _BASE64URL.fullmatch(value) is None:
        raise authentication_required()
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise authentication_required() from error
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise authentication_required()
    return decoded


def _jwt_parts(
    token: str,
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise authentication_required()
    try:
        header = json.loads(
            _b64url(parts[0], maximum=4_000),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        claims = json.loads(
            _b64url(parts[1], maximum=16_000),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as error:
        raise authentication_required() from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise authentication_required()
    signature = _b64url(parts[2], maximum=4_000)
    return (
        cast(dict[str, Any], header),
        cast(dict[str, Any], claims),
        f"{parts[0]}.{parts[1]}".encode("ascii"),
        signature,
    )


def _verify_rs256(key: Mapping[str, Any], signed: bytes, signature: bytes) -> None:
    exponent_value = key.get("e")
    modulus_value = key.get("n")
    if not isinstance(exponent_value, str) or not isinstance(modulus_value, str):
        raise authentication_required()
    exponent = int.from_bytes(_b64url(exponent_value, maximum=16), "big")
    modulus = int.from_bytes(_b64url(modulus_value, maximum=2_000), "big")
    if exponent not in {3, 65_537} or modulus.bit_length() < 2_048:
        raise authentication_required()
    try:
        rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
            signature, signed, padding.PKCS1v15(), hashes.SHA256()
        )
    except (ValueError, InvalidSignature) as error:
        raise authentication_required() from error


def _verify_audience(claims: Mapping[str, Any], client_id: str) -> None:
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if (
        not isinstance(audiences, list)
        or not audiences
        or len(audiences) > 20
        or any(
            not isinstance(item, str) or not 1 <= len(item) <= 2_000
            for item in audiences
        )
        or len(set(audiences)) != len(audiences)
        or client_id not in audiences
    ):
        raise authentication_required()
    authorized_party = claims.get("azp")
    if (
        len(audiences) > 1 or authorized_party is not None
    ) and authorized_party != client_id:
        raise authentication_required()


def _verify_time_and_nonce(claims: Mapping[str, Any], nonce: str) -> None:
    expires_at = claims.get("exp")
    not_before = claims.get("nbf")
    issued_at = claims.get("iat")
    now = datetime.now(tz=UTC).timestamp()
    if (
        not _is_finite_numeric_date(expires_at)
        or expires_at <= now
        or claims.get("nonce") != nonce
        or (not_before is not None and not _is_finite_numeric_date(not_before))
        or not _is_finite_numeric_date(issued_at)
        or (_is_finite_numeric_date(not_before) and not_before > now)
        or issued_at > now + 60
        or issued_at > expires_at
    ):
        raise authentication_required()


def _is_finite_numeric_date(value: object) -> TypeIs[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= _MAXIMUM_NUMERIC_DATE
    return math.isfinite(value) and abs(value) <= _MAXIMUM_NUMERIC_DATE


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = "An OpenID Connect JSON object has a duplicate field."
            raise ValueError(message)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    message = f"The OpenID Connect JSON constant {value!r} is invalid."
    raise ValueError(message)

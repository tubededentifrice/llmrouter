"""Exact OpenID Connect authorization-code and identity-token validation."""
# ruff: noqa: C901, EM101, N818, PLR2004, S105, TRY301

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
from Crypto.Hash import SHA256  # nosec B413 - This import is maintained PyCryptodome.
from Crypto.PublicKey import RSA  # nosec B413 - This import is maintained PyCryptodome.
from Crypto.Signature import pkcs1_15  # nosec B413 - Maintained PyCryptodome.

from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.model import (
    IdentityState,
    OIDCTokenResponse,
    ProviderSessionState,
    SecretValue,
    VerifiedIdentity,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from Crypto.PublicKey.RSA import RsaKey  # nosec B413 - Maintained PyCryptodome.

_AUTHORIZATION_PARAMETERS = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
        "max_age",
        "prompt",
    }
)


class IdentityServiceUnavailable(RuntimeError):
    """The configured identity service did not give an authoritative result."""


class ProviderSessionInvalid(RuntimeError):
    """The identity provider rejected the stored provider session."""


class ProviderSessionRotationFailed(IdentityServiceUnavailable):
    """A refresh rotated the provider secret before a later safe failure."""


class IdentityService(Protocol):
    """The network operations required from the shared identity service."""

    def available(self) -> bool:
        """Return true only after an authoritative availability check."""
        ...

    def exchange_code(
        self, *, code: str, redirect_uri: str, pkce_verifier: str
    ) -> OIDCTokenResponse:
        """Exchange one code through the confidential client."""
        ...

    def account_state(self, *, issuer: str, subject: str) -> IdentityState:
        """Return current disablement and recovery state."""
        ...

    def provider_session_state(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        now: datetime,
    ) -> ProviderSessionState:
        """Return authoritative central-session state and rotated tokens."""
        ...


@dataclass(frozen=True, slots=True)
class OIDCConfiguration:
    """Exact deployment values for one confidential Router client."""

    issuer: str
    client_id: str
    authorization_endpoint: str
    redirect_uri: str
    account_url: str
    signing_algorithm: str

    def __post_init__(self) -> None:
        """Reject partial or non-HTTPS production identity configuration."""
        values = (
            self.issuer,
            self.client_id,
            self.authorization_endpoint,
            self.redirect_uri,
            self.account_url,
        )
        if any(not value for value in values):
            msg = "Each OpenID Connect configuration value is required."
            raise ValueError(msg)
        if any(
            urlsplit(value).scheme != "https"
            or not urlsplit(value).hostname
            or urlsplit(value).username is not None
            or urlsplit(value).password is not None
            for value in (
                self.issuer,
                self.authorization_endpoint,
                self.redirect_uri,
                self.account_url,
            )
        ):
            msg = "OpenID Connect endpoints must use HTTPS."
            raise ValueError(msg)
        authorization = urlsplit(self.authorization_endpoint)
        if authorization.fragment:
            msg = "The authorization endpoint must not contain a fragment."
            raise ValueError(msg)
        if any(
            key in _AUTHORIZATION_PARAMETERS
            for key, _ in parse_qsl(authorization.query, keep_blank_values=True)
        ):
            msg = "The authorization endpoint contains a reserved request parameter."
            raise ValueError(msg)
        issuer = urlsplit(self.issuer)
        if issuer.query or issuer.fragment:
            msg = "The identity issuer must not contain a query or fragment."
            raise ValueError(msg)
        redirect = urlsplit(self.redirect_uri)
        if redirect.fragment:
            msg = "The redirect URI must not contain a fragment."
            raise ValueError(msg)
        account = urlsplit(self.account_url)
        if account.fragment:
            msg = "The identity account URL must not contain a fragment."
            raise ValueError(msg)
        if self.signing_algorithm != "RS256":
            msg = "The identity-token signing algorithm must be RS256."
            raise ValueError(msg)


class OIDCTokenVerifier:
    """Verify one ID token against an exact issuer, client, key, and algorithm."""

    def __init__(
        self,
        configuration: OIDCConfiguration,
        signing_keys: Mapping[str, bytes | str],
    ) -> None:
        """Use a nonempty trusted key set indexed by exact key identity."""
        if not signing_keys or any(
            not 1 <= len(key_id) <= 200 for key_id in signing_keys
        ):
            msg = "At least one named identity signing key is required."
            raise ValueError(msg)
        self._configuration = configuration
        validated_keys: dict[str, RsaKey] = {}
        try:
            for key_id, encoded_key in signing_keys.items():
                key = RSA.import_key(encoded_key)
                if key.has_private() or key.size_in_bits() < 2048:
                    msg = (
                        "Identity signing keys must be public RSA keys of 2048 "
                        "bits or more."
                    )
                    raise ValueError(msg)
                validated_keys[key_id] = key
        except (IndexError, TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith(
                "Identity signing keys"
            ):
                raise
            msg = "Each identity signing key must be a valid public RSA key."
            raise ValueError(msg) from error
        self._signing_keys = validated_keys

    def verify(
        self,
        response: OIDCTokenResponse,
        *,
        expected_nonce: str,
        now: datetime,
        request_id: str,
    ) -> VerifiedIdentity:
        """Validate token use, header, signature, exact claims, and time."""
        if (
            response.token_type != "Bearer"  # nosec B105
            or not 1 <= len(response.id_token) <= 16384
            or not 32 <= len(expected_nonce) <= 200
        ):
            raise AdministratorAuthError("invalid_token", request_id)
        try:
            header = jwt.get_unverified_header(response.id_token)
            key_id = header.get("kid")
            if (
                header.get("alg") != self._configuration.signing_algorithm
                or (header.get("typ") is not None and header.get("typ") != "JWT")
                or not isinstance(key_id, str)
                or key_id not in self._signing_keys
            ):
                raise AdministratorAuthError("invalid_token", request_id)
            signing_input, encoded_signature = response.id_token.rsplit(".", 1)
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
            pkcs1_15.new(self._signing_keys[key_id]).verify(
                SHA256.new(signing_input.encode()), signature
            )
            claims = jwt.decode(
                response.id_token,
                algorithms=[self._configuration.signing_algorithm],
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "exp",
                        "iat",
                        "auth_time",
                        "nonce",
                        "type",
                    ],
                },
            )
        except AdministratorAuthError:
            raise
        except (ValueError, TypeError, jwt.PyJWTError) as error:
            raise AdministratorAuthError("invalid_token", request_id) from error
        audience = claims["aud"]
        if (
            claims["iss"] != self._configuration.issuer
            or audience != self._configuration.client_id
        ):
            raise AdministratorAuthError("invalid_token", request_id)
        if "azp" in claims and claims["azp"] != self._configuration.client_id:
            raise AdministratorAuthError("invalid_token", request_id)
        if claims["type"] != "id-token":
            raise AdministratorAuthError("invalid_token", request_id)
        if (
            not isinstance(claims["nonce"], str)
            or not hmac.compare_digest(claims["nonce"], expected_nonce)
            or not isinstance(claims["sub"], str)
            or not 1 <= len(claims["sub"]) <= 200
        ):
            raise AdministratorAuthError("invalid_token", request_id)
        numeric_names = ["iat", "exp", "auth_time"]
        if "nbf" in claims:
            numeric_names.append("nbf")
        if any(
            isinstance(claims[name], bool) or not isinstance(claims[name], int)
            for name in numeric_names
        ):
            raise AdministratorAuthError("invalid_token", request_id)
        try:
            issued_at = datetime.fromtimestamp(claims["iat"], tz=UTC)
            expires_at = datetime.fromtimestamp(claims["exp"], tz=UTC)
            authenticated_at = datetime.fromtimestamp(claims["auth_time"], tz=UTC)
            not_before = datetime.fromtimestamp(
                claims.get("nbf", claims["iat"]), tz=UTC
            )
        except (OverflowError, OSError, ValueError) as error:
            raise AdministratorAuthError("invalid_token", request_id) from error
        if (
            not_before > now
            or issued_at > now
            or authenticated_at > now
            or expires_at <= now
            or authenticated_at > issued_at
        ):
            raise AdministratorAuthError("invalid_token", request_id)
        return VerifiedIdentity(
            issuer=claims["iss"],
            subject=claims["sub"],
            nonce=claims["nonce"],
            issued_at=issued_at,
            expires_at=expires_at,
            authenticated_at=authenticated_at,
        )


def build_authorization_url(
    configuration: OIDCConfiguration,
    *,
    state: str,
    nonce: str,
    pkce_verifier: str,
    recent_authentication: bool = False,
) -> str:
    """Build the exact authorization-code request with PKCE S256."""
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    parameters: dict[str, str] = {
        "response_type": "code",
        "client_id": configuration.client_id,
        "redirect_uri": configuration.redirect_uri,
        "scope": "openid offline_access",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if recent_authentication:
        parameters.update({"max_age": "0", "prompt": "login"})
    endpoint = urlsplit(configuration.authorization_endpoint)
    query = urlencode(
        [*parse_qsl(endpoint.query, keep_blank_values=True), *parameters.items()]
    )
    return urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, query, ""))


def administrator_session_cookie(value: str, *, clear: bool = False) -> str:
    """Build the exact host-only secure local session cookie."""
    if not clear:
        try:
            SecretValue(value)
        except ValueError as error:
            msg = "A local session cookie needs one generated token."
            raise ValueError(msg) from error
    cookie = f"__Host-llmrouter-admin={'' if clear else value}; Path=/"
    if clear:
        cookie += "; Max-Age=0"
    return f"{cookie}; Secure; HttpOnly; SameSite=Lax"

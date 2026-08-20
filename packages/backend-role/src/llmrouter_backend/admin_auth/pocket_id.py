"""Bounded HTTP adapter for the shared Pocket ID service."""
# ruff: noqa: ANN401, D102, D107, EM101, PLR0913, PLR2004, TRY003, TRY004, TRY300, TRY301

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from Crypto.PublicKey import RSA  # nosec B413 - Maintained PyCryptodome.

from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.model import (
    IdentityState,
    OIDCTokenResponse,
    ProviderSessionState,
    VerifiedIdentity,
)
from llmrouter_backend.admin_auth.oidc import (
    IdentityServiceUnavailable,
    OIDCConfiguration,
    OIDCTokenVerifier,
    ProviderSessionInvalid,
    ProviderSessionRotationFailed,
)

_MAXIMUM_RESPONSE_BYTES = 65_536
_MAXIMUM_PROVIDER_TOKEN_SECONDS = 86_400


class PocketIDIdentityService:
    """Call only the exact Pocket ID token and administrator API endpoints."""

    def __init__(
        self,
        configuration: OIDCConfiguration,
        *,
        token_endpoint: str,
        jwks_endpoint: str,
        introspection_endpoint: str,
        api_base_url: str,
        client_secret: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not client_secret or not api_key:
            raise ValueError("Pocket ID deployment secrets are required.")
        issuer = configuration.issuer.rstrip("/")
        expected = {
            f"{issuer}/api/oidc/token",
            f"{issuer}/.well-known/jwks.json",
            f"{issuer}/api/oidc/introspect",
            f"{issuer}/api",
        }
        if {
            token_endpoint,
            jwks_endpoint,
            introspection_endpoint,
            api_base_url.rstrip("/"),
        } != expected:
            raise ValueError("Pocket ID endpoints must use the exact issuer.")
        self._configuration = configuration
        self._token_endpoint = token_endpoint
        self._jwks_endpoint = jwks_endpoint
        self._introspection_endpoint = introspection_endpoint
        self._api_base_url = api_base_url.rstrip("/")
        self._client_secret = client_secret
        self._api_key = api_key
        self._client = httpx.Client(
            timeout=httpx.Timeout(5.0), follow_redirects=False, transport=transport
        )
        self._verifier: OIDCTokenVerifier | None = None

    def available(self) -> bool:
        self._request("GET", self._jwks_endpoint)
        return True

    def exchange_code(
        self, *, code: str, redirect_uri: str, pkce_verifier: str
    ) -> OIDCTokenResponse:
        document = self._request(
            "POST",
            self._token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": pkce_verifier,
            },
            auth=(self._configuration.client_id, self._client_secret),
        )
        try:
            return OIDCTokenResponse(
                id_token=_string(document, "id_token"),
                token_type=_string(document, "token_type"),
                access_token=_optional_token(document, "access_token"),
                refresh_token=_optional_token(document, "refresh_token"),
                expires_in=_optional_positive_integer(document, "expires_in"),
            )
        except (TypeError, ValueError) as error:
            raise IdentityServiceUnavailable from error

    def provider_session_state(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        now: datetime,
    ) -> ProviderSessionState:
        if not access_token or not refresh_token or access_expires_at.tzinfo is None:
            raise IdentityServiceUnavailable
        rotated = False
        if access_expires_at <= now:
            document = self._request(
                "POST",
                self._token_endpoint,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(self._configuration.client_id, self._client_secret),
                invalid_provider_session=True,
            )
            prior_refresh_token = refresh_token
            rotated = True
            try:
                if _string(document, "token_type").casefold() != "bearer":
                    raise ValueError
                access_token = _token(document, "access_token")
                refresh_token = _token(document, "refresh_token")
                if refresh_token == prior_refresh_token:
                    raise ValueError
                expires_in = _positive_integer(document, "expires_in")
                access_expires_at = now + timedelta(seconds=expires_in)
            except (TypeError, ValueError) as error:
                raise ProviderSessionRotationFailed from error
        try:
            document = self._request(
                "POST",
                self._introspection_endpoint,
                data={"token": access_token},
                auth=(self._configuration.client_id, self._client_secret),
                invalid_provider_session=True,
            )
        except (IdentityServiceUnavailable, ProviderSessionInvalid) as error:
            if rotated:
                raise ProviderSessionRotationFailed from error
            raise
        if not isinstance(document.get("active"), bool):
            if rotated:
                raise ProviderSessionRotationFailed
            raise IdentityServiceUnavailable
        return ProviderSessionState(
            active=document["active"],
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            checked_at=now,
        )

    def account_state(
        self, *, issuer: str, subject: str, now: datetime
    ) -> IdentityState:
        if issuer != self._configuration.issuer or not subject:
            raise IdentityServiceUnavailable
        encoded_subject = quote(subject, safe="")
        headers = {"X-API-KEY": self._api_key}
        user = self._request(
            "GET", f"{self._api_base_url}/users/{encoded_subject}", headers=headers
        )
        credentials = self._request(
            "GET",
            f"{self._api_base_url}/users/{encoded_subject}/webauthn-credentials",
            headers=headers,
            allow_array=True,
        )
        try:
            if user.get("id") != subject or not isinstance(user.get("disabled"), bool):
                raise ValueError
            values = credentials
            if len(values) > 1000:
                raise ValueError
            identifiers = sorted(_string(item, "id") for item in values)
            if any(len(identifier) > 200 for identifier in identifiers):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise IdentityServiceUnavailable from error
        fingerprint = hashlib.sha256(
            json.dumps(identifiers, separators=(",", ":")).encode()
        ).digest()
        generation = int.from_bytes(fingerprint[:8], "big") & ((1 << 63) - 1)
        return IdentityState(
            active=not user["disabled"],
            generation=max(1, generation),
            checked_at=now,
        )

    def token_verifier(self) -> PocketIDTokenVerifier:
        return PocketIDTokenVerifier(self)

    def _load_verifier(self) -> OIDCTokenVerifier:
        document = self._request("GET", self._jwks_endpoint)
        try:
            keys: dict[str, bytes] = {}
            values = document["keys"]
            if not isinstance(values, list) or not 1 <= len(values) <= 100:
                raise ValueError
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError
                if value.get("kty") != "RSA" or value.get("alg") not in {None, "RS256"}:
                    continue
                key_id = _string(value, "kid")
                modulus_value = _string(value, "n")
                exponent_value = _string(value, "e")
                if (
                    len(key_id) > 200
                    or len(modulus_value) > 1024
                    or len(exponent_value) > 16
                ):
                    raise ValueError
                modulus = int.from_bytes(_decode(modulus_value), "big")
                exponent = int.from_bytes(_decode(exponent_value), "big")
                keys[key_id] = (
                    RSA.construct((modulus, exponent)).public_key().export_key()
                )
            verifier = OIDCTokenVerifier(self._configuration, keys)
        except (KeyError, TypeError, ValueError) as error:
            raise IdentityServiceUnavailable from error
        self._verifier = verifier
        return verifier

    def _request(
        self,
        method: str,
        url: str,
        *,
        allow_array: bool = False,
        invalid_provider_session: bool = False,
        **kwargs: Any,
    ) -> Any:
        try:
            with self._client.stream(method, url, **kwargs) as response:
                if response.is_redirect:
                    raise IdentityServiceUnavailable
                if response.status_code != 200:
                    if invalid_provider_session and response.status_code in {
                        400,
                        401,
                        403,
                    }:
                        raise ProviderSessionInvalid
                    raise IdentityServiceUnavailable
                content_types = response.headers.get_list("content-type")
                media_type = (
                    content_types[0].partition(";")[0].lower() if content_types else ""
                )
                if len(content_types) != 1 or media_type not in {
                    "application/json",
                    "application/jwk-set+json",
                }:
                    raise IdentityServiceUnavailable
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAXIMUM_RESPONSE_BYTES:
                        raise IdentityServiceUnavailable
            document = json.loads(content, object_pairs_hook=_bounded_object)
            if not isinstance(document, dict) and not (
                allow_array and isinstance(document, list)
            ):
                raise ValueError
            return document
        except (httpx.HTTPError, ValueError, UnicodeError) as error:
            raise IdentityServiceUnavailable from error


class PocketIDTokenVerifier:
    """Refresh Pocket ID signing keys once when a token key is not current."""

    def __init__(self, identity: PocketIDIdentityService) -> None:
        self._identity = identity

    def verify(
        self,
        response: OIDCTokenResponse,
        *,
        expected_nonce: str,
        now: datetime,
        request_id: str,
    ) -> VerifiedIdentity:
        verifier = self._identity._verifier or self._identity._load_verifier()  # noqa: SLF001
        try:
            return verifier.verify(
                response,
                expected_nonce=expected_nonce,
                now=now,
                request_id=request_id,
            )
        except AdministratorAuthError:
            return self._identity._load_verifier().verify(  # noqa: SLF001
                response,
                expected_nonce=expected_nonce,
                now=now,
                request_id=request_id,
            )


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _bounded_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) > 100:
        raise ValueError
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _string(document: Any, key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _token(document: Any, key: str) -> str:
    value = _string(document, key)
    if len(value) > 8192:
        raise ValueError
    return value


def _optional_token(document: Any, key: str) -> str | None:
    if key not in document:
        return None
    return _token(document, key)


def _positive_integer(document: Any, key: str) -> int:
    value = document[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAXIMUM_PROVIDER_TOKEN_SECONDS
    ):
        raise ValueError
    return value


def _optional_positive_integer(document: Any, key: str) -> int | None:
    if key not in document:
        return None
    return _positive_integer(document, key)

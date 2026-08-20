"""Focused Pocket ID HTTP adapter tests."""
# ruff: noqa: D103, PLR2004, S105, S106

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from Crypto.PublicKey import RSA
from llmrouter_backend.admin_auth import OIDCConfiguration
from llmrouter_backend.admin_auth.oidc import (
    IdentityServiceUnavailable,
    ProviderSessionInvalid,
    ProviderSessionRotationFailed,
)
from llmrouter_backend.admin_auth.pocket_id import PocketIDIdentityService

ISSUER = "https://auth.opendle.dev"


def _configuration() -> OIDCConfiguration:
    return OIDCConfiguration(
        issuer=ISSUER,
        client_id="router-client",
        authorization_endpoint=f"{ISSUER}/authorize",
        redirect_uri="https://llmrouter.opendle.dev/v1/admin/oidc/callback",
        account_url=f"{ISSUER}/settings/account",
        signing_algorithm="RS256",
    )


def _identity(handler) -> PocketIDIdentityService:  # noqa: ANN001
    return PocketIDIdentityService(
        _configuration(),
        token_endpoint=f"{ISSUER}/api/oidc/token",
        jwks_endpoint=f"{ISSUER}/.well-known/jwks.json",
        introspection_endpoint=f"{ISSUER}/api/oidc/introspect",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )


def test_code_exchange_uses_confidential_client_and_pkce() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{ISSUER}/api/oidc/token"
        expected = base64.b64encode(b"router-client:client-secret").decode()
        assert request.headers["Authorization"] == f"Basic {expected}"
        assert b"code_verifier=verifier" in request.content
        return httpx.Response(
            200,
            json={
                "id_token": "header.payload.signature",
                "access_token": "transient-access-token",
                "refresh_token": "transient-refresh-token",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    result = _identity(handler).exchange_code(
        code="code",
        redirect_uri="https://llmrouter.opendle.dev/v1/admin/oidc/callback",
        pkce_verifier="verifier",
    )
    assert result.token_type == "Bearer"
    assert "transient-access-token" not in repr(result)


def test_provider_session_rotates_refresh_and_introspects() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/token"):
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 300,
                    "token_type": "Bearer",
                },
            )
        assert request.url.path.endswith("/introspect")
        assert b"token=rotated-access" in request.content
        return httpx.Response(200, json={"active": True})

    now = datetime(2026, 8, 20, tzinfo=UTC)
    state = _identity(handler).provider_session_state(
        access_token="current-access",
        refresh_token="refresh-token",
        access_expires_at=now + timedelta(minutes=5),
        now=now,
    )
    assert state.active
    assert state.access_expires_at == now + timedelta(minutes=5)
    assert requests == ["/api/oidc/token", "/api/oidc/introspect"]


def test_rejected_refresh_is_an_invalid_provider_session() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    with pytest.raises(ProviderSessionInvalid):
        _identity(
            lambda _request: httpx.Response(401, json={"error": "invalid"})
        ).provider_session_state(
            access_token="expired-access",
            refresh_token="revoked-refresh",
            access_expires_at=now,
            now=now,
        )


def test_failure_after_refresh_rotation_requires_local_revocation() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 300,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(503, json={"error": "offline"})

    with pytest.raises(ProviderSessionRotationFailed):
        _identity(handler).provider_session_state(
            access_token="expired-access",
            refresh_token="old-refresh",
            access_expires_at=now,
            now=now,
        )


@pytest.mark.parametrize(
    "response", [httpx.Response(302), httpx.Response(200, content=b"x" * 65537)]
)
def test_adapter_rejects_redirects_and_oversized_responses(
    response: httpx.Response,
) -> None:
    with pytest.raises(IdentityServiceUnavailable):
        _identity(lambda _request: response).available()


def test_adapter_stops_streaming_at_the_response_limit() -> None:
    read_chunks = 0

    class Stream(httpx.SyncByteStream):
        def __iter__(self):  # noqa: ANN204
            nonlocal read_chunks
            for _ in range(100):
                read_chunks += 1
                yield b"x" * 1024

    with pytest.raises(IdentityServiceUnavailable):
        _identity(
            lambda _request: httpx.Response(
                200, stream=Stream(), headers={"Content-Type": "application/json"}
            )
        ).available()
    assert read_chunks == 65


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{}", headers={"Content-Type": "text/plain"}),
        httpx.Response(
            200,
            content=b'{"keys":[],"keys":[]}',
            headers={"Content-Type": "application/json"},
        ),
        httpx.Response(200, json=[]),
    ],
)
def test_adapter_rejects_untrusted_json_shapes(response: httpx.Response) -> None:
    with pytest.raises(IdentityServiceUnavailable):
        _identity(lambda _request: response).available()


def test_jwks_loader_accepts_only_public_rs256_keys() -> None:
    key = RSA.generate(2048).public_key()
    numbers = key.n, key.e

    def encoded(value: int) -> str:
        data = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "kid": "current",
                "n": encoded(numbers[0]),
                "e": encoded(numbers[1]),
            }
        ]
    }
    identity = _identity(
        lambda _request: httpx.Response(
            200,
            content=json.dumps(jwks),
            headers={"Content-Type": "application/json"},
        )
    )
    assert identity._load_verifier() is not None  # noqa: SLF001


def test_jwks_loader_rejects_oversized_rsa_components() -> None:
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "kid": "current",
                "n": "A" * 1025,
                "e": "AQAB",
            }
        ]
    }
    with pytest.raises(IdentityServiceUnavailable):
        _identity(lambda _request: httpx.Response(200, json=jwks))._load_verifier()  # noqa: SLF001

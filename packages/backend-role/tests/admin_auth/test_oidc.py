"""Standards-correct Pocket ID token-shape tests."""
# ruff: noqa: D103

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from llmrouter_backend.admin_auth import (
    AdministratorAuthError,
    OIDCConfiguration,
    OIDCTokenResponse,
    OIDCTokenVerifier,
)

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
ISSUER = "https://auth.opendle.dev"
CLIENT_ID = "router-client"
NONCE = "N" * 43


def _verifier_and_key() -> tuple[OIDCTokenVerifier, RSA.RsaKey]:
    key = RSA.generate(2048)
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        authorization_endpoint=f"{ISSUER}/authorize",
        redirect_uri="https://llmrouter.opendle.dev/v1/admin/oidc/callback",
        account_url=f"{ISSUER}/account",
        signing_algorithm="RS256",
    )
    return (
        OIDCTokenVerifier(configuration, {"pocket-key": key.public_key().export_key()}),
        key,
    )


def _token(key: RSA.RsaKey, **overrides: object) -> str:
    header = {"alg": "RS256", "kid": "pocket-key", "typ": "JWT"}
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "user-id",
        "aud": [CLIENT_ID],
        "azp": CLIENT_ID,
        "type": "id-token",
        "nonce": NONCE,
        "iat": NOW.timestamp(),
        "exp": (NOW + timedelta(minutes=5)).timestamp(),
        "auth_time": NOW.timestamp(),
    }
    claims.update(overrides)

    def encode(value: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

    signing_input = f"{encode(header)}.{encode(claims)}"
    signature = pkcs1_15.new(key).sign(SHA256.new(signing_input.encode()))
    return (
        f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    )


def _verify(token_type: object, **claims: object) -> None:
    verifier, key = _verifier_and_key()
    verifier.verify(
        OIDCTokenResponse(
            id_token=_token(key, **claims),
            token_type=token_type,  # type: ignore[arg-type]
        ),
        expected_nonce=NONCE,
        now=NOW,
        request_id="test-request",
    )


def test_pocket_id_standard_token_shape_is_accepted() -> None:
    _verify("bearer")


@pytest.mark.parametrize("token_type", ["Bearer", "BEARER", "bEaReR"])
def test_bearer_token_type_is_case_insensitive(token_type: str) -> None:
    _verify(token_type)


@pytest.mark.parametrize(
    "audience",
    [[], [CLIENT_ID, "other-client"], [CLIENT_ID, CLIENT_ID]],
)
def test_non_exact_audience_arrays_are_rejected(audience: list[str]) -> None:
    with pytest.raises(AdministratorAuthError) as error:
        _verify("bearer", aud=audience)
    assert error.value.code == "invalid_token"


@pytest.mark.parametrize("numeric_date", [True, "1786626000", float("nan")])
def test_invalid_numeric_dates_are_rejected(numeric_date: object) -> None:
    with pytest.raises(AdministratorAuthError) as error:
        _verify("bearer", iat=numeric_date)
    assert error.value.code == "invalid_token"


def test_invalid_token_type_is_rejected() -> None:
    with pytest.raises(AdministratorAuthError) as error:
        _verify("MAC")
    assert error.value.code == "invalid_token"

"""Create the public Pocket ID administrator authority from file secrets."""
# ruff: noqa: EM101, PLR2004, TRY003

from __future__ import annotations

import os
import re
import stat
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from llmrouter_backend.admin_auth.oidc import OIDCConfiguration
from llmrouter_backend.admin_auth.pocket_id import PocketIDIdentityService
from llmrouter_backend.admin_auth.repository import AdministratorAuthRepository

ISSUER = "https://auth.opendle.dev"
ORIGIN = "https://llmrouter.opendle.dev"
CALLBACK = f"{ORIGIN}/v1/admin/oidc/callback"


def configured_repository(database_url: str) -> AdministratorAuthRepository | None:
    """Return no authority only when public authentication is explicitly off."""
    enabled = os.environ.get("LLMROUTER_PUBLIC_ADMIN_AUTH", "0")
    if enabled == "0":
        return None
    if enabled != "1":
        raise RuntimeError("The public administrator authentication flag is invalid.")
    names = {
        "client_id": "LLMROUTER_OIDC_CLIENT_ID_FILE",
        "client_secret": "LLMROUTER_OIDC_CLIENT_SECRET_FILE",
        "digest_key": "LLMROUTER_ADMIN_DIGEST_KEY_FILE",
        "encryption_key": "LLMROUTER_ADMIN_ENCRYPTION_KEY_FILE",
    }
    paths = {key: os.environ.get(name) for key, name in names.items()}
    if any(value is None for value in paths.values()):
        raise RuntimeError("The public administrator authentication is incomplete.")
    raw_values = {key: _read(Path(value or "")) for key, value in paths.items()}
    values = {
        "client_id": _client_id(raw_values["client_id"]),
        "client_secret": _pocket_secret(raw_values["client_secret"]),
        "digest_key": raw_values["digest_key"],
        "encryption_key": raw_values["encryption_key"],
    }
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=values["client_id"],
        authorization_endpoint=f"{ISSUER}/authorize",
        redirect_uri=CALLBACK,
        account_url=f"{ISSUER}/settings/account",
        signing_algorithm="RS256",
    )
    identity = PocketIDIdentityService(
        configuration,
        token_endpoint=f"{ISSUER}/api/oidc/token",
        jwks_endpoint=f"{ISSUER}/.well-known/jwks.json",
        introspection_endpoint=f"{ISSUER}/api/oidc/introspect",
        client_secret=values["client_secret"],
    )
    return AdministratorAuthRepository(
        database_url,
        configuration=configuration,
        identity_service=identity,
        token_verifier=identity.token_verifier(),  # type: ignore[arg-type]
        digest_key=_key(values["digest_key"]),
        encryption_key=_key(values["encryption_key"]),
        exact_origin=ORIGIN,
        trusted_grant_base_url=f"{ORIGIN}/trusted-grant",
    )


def _client_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._~-]{1,200}", value) is None:
        raise RuntimeError("The Pocket ID client ID is invalid.")
    return value


def _pocket_secret(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]{32}", value) is None:
        raise RuntimeError("A Pocket ID client secret is invalid.")
    return value


def _read(path: Path) -> str:
    unavailable = "An administrator authentication secret is unavailable."
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb", closefd=True) as secret:
            metadata = os.fstat(secret.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(unavailable)
            value = secret.read(4097).decode("ascii")
            if len(value) > 4096 or "\r" in value:
                raise RuntimeError(unavailable)
            value = value.removesuffix("\n")
            if "\n" in value:
                raise RuntimeError(unavailable)
            return value
    except (OSError, UnicodeError) as error:
        raise RuntimeError(unavailable) from error


def _key(value: str) -> bytes:
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise RuntimeError("An administrator authentication key is invalid.") from error
    if len(decoded) != 32:
        raise RuntimeError("An administrator authentication key is invalid.")
    if urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
        raise RuntimeError("An administrator authentication key is invalid.")
    return decoded

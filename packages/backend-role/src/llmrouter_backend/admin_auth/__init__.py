"""Pocket ID authentication and least-privilege local administrator grants."""

from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.model import (
    AuthenticationPurpose,
    AuthorizationStart,
    GrantRequest,
    GrantState,
    OIDCTokenResponse,
    ProviderSessionState,
    SessionResult,
    TrustedGrantPurpose,
    TrustedGrantURL,
)
from llmrouter_backend.admin_auth.oidc import (
    IdentityService,
    IdentityServiceUnavailable,
    OIDCConfiguration,
    OIDCTokenVerifier,
    ProviderSessionInvalid,
    ProviderSessionRotationFailed,
    administrator_session_cookie,
)
from llmrouter_backend.admin_auth.repository import AdministratorAuthRepository

__all__ = [
    "AdministratorAuthError",
    "AdministratorAuthRepository",
    "AuthenticationPurpose",
    "AuthorizationStart",
    "GrantRequest",
    "GrantState",
    "IdentityService",
    "IdentityServiceUnavailable",
    "OIDCConfiguration",
    "OIDCTokenResponse",
    "OIDCTokenVerifier",
    "ProviderSessionInvalid",
    "ProviderSessionRotationFailed",
    "ProviderSessionState",
    "SessionResult",
    "TrustedGrantPurpose",
    "TrustedGrantURL",
    "administrator_session_cookie",
]

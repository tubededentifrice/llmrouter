"""PostgreSQL authority for Pocket ID sessions and local administrator grants."""
# ruff: noqa: ANN401, ARG002, C901, E501, EM101, PIE810, PLR0912, PLR0913, PLR0915, PLR0917, PLR2004, RUF100, S101, S105, S608, TRY003, TRY203, TRY301

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import jwt
import psycopg
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError
from psycopg.rows import dict_row

from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.model import (
    AdministratorGrant,
    AuthenticationPurpose,
    AuthorizationStart,
    GrantRequest,
    GrantState,
    SecretValue,
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
    build_authorization_url,
)
from llmrouter_backend.authority import (
    ACCOUNT_STATE_LIMIT,
    ADMINISTRATOR_ABSOLUTE_LIMIT,
    ADMINISTRATOR_IDLE_LIMIT,
    ADMINISTRATOR_OPERATIONS,
    RECENT_AUTH_LIMIT,
    AdministratorPrincipal,
    AuthorityClass,
    AuthorityPath,
    BrowserWriteProof,
    OperationPolicy,
    RequestContext,
    Scope,
    authorize,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from psycopg import Connection

OIDC_START_LIFETIME = timedelta(minutes=5)
TRUSTED_GRANT_URL_LIMIT = timedelta(minutes=15)
_ENCRYPTION_DOMAIN = b"llmrouter-admin-oidc-pkce-v1\x00"
_RETURN_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$")
_MINIMUM_IDEMPOTENCY_LENGTH = 16
_MAXIMUM_IDEMPOTENCY_LENGTH = 200


class AdministratorAuthRepository:
    """Keep human authentication separate from local Router authorization."""

    def __init__(  # noqa: PLR0913
        self,
        database_url: str,
        *,
        configuration: OIDCConfiguration,
        identity_service: IdentityService,
        token_verifier: OIDCTokenVerifier,
        digest_key: bytes,
        encryption_key: bytes,
        exact_origin: str,
        trusted_grant_base_url: str,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        """Use deployment-held keys and exact browser and identity endpoints."""
        if len(digest_key) != 32 or len(encryption_key) != 32:
            msg = "Administrator digest and encryption keys must contain 32 bytes."
            raise ValueError(msg)
        if hmac.compare_digest(digest_key, encryption_key):
            msg = "Administrator digest and encryption keys must be different."
            raise ValueError(msg)
        parsed_origin = urlsplit(exact_origin)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            msg = "The administrator origin must be an exact HTTPS origin."
            raise ValueError(msg)
        parsed_trusted_url = urlsplit(trusted_grant_base_url)
        if (
            parsed_trusted_url.scheme != parsed_origin.scheme
            or parsed_trusted_url.netloc != parsed_origin.netloc
            or parsed_trusted_url.username is not None
            or parsed_trusted_url.password is not None
            or not parsed_trusted_url.path.startswith("/")
            or parsed_trusted_url.path.startswith("//")
            or parsed_trusted_url.fragment
            or any(key == "token" for key, _ in parse_qsl(parsed_trusted_url.query))
        ):
            msg = "The trusted grant URL must use the exact administrator origin."
            raise ValueError(msg)
        self._database_url = database_url
        self.configuration = configuration
        self._identity_service = identity_service
        self._token_verifier = token_verifier
        self._digest_key = digest_key
        self._encryption_key = encryption_key
        self._exact_origin = exact_origin
        self._trusted_grant_base_url = trusted_grant_base_url
        self._identity_factory = identity_factory
        self._random_bytes = random_bytes

    def create_trusted_grant_url(
        self,
        purpose: TrustedGrantPurpose,
        operations: frozenset[str],
        *,
        request_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> TrustedGrantURL:
        """Create one short-lived global grant URL from a trusted console."""
        self._require_time(now)
        try:
            if (
                not operations
                or not operations <= ADMINISTRATOR_OPERATIONS
                or expires_at <= now
                or expires_at - now > TRUSTED_GRANT_URL_LIMIT
            ):
                raise AdministratorAuthError("invalid_request", request_id)
            token = self._secret()
            token_digest = self._digest("trusted-grant", token.value)
            url_id = self._identity_factory()
            with self._connect() as connection, connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("administrator-trusted-grant",),
                )
                eligible = self._eligible_global_grant_exists(connection, now)
                any_grant = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM router.administrator_grants) AS found"
                ).fetchone()
                if (purpose is TrustedGrantPurpose.RECOVERY and eligible) or (
                    purpose is TrustedGrantPurpose.INITIAL
                    and any_grant
                    and any_grant["found"]
                ):
                    raise AdministratorAuthError("insufficient_scope", request_id)
                connection.execute(
                    """
                    INSERT INTO router.trusted_administrator_grant_urls (
                        id, verifier_digest, purpose, operations, created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        url_id,
                        token_digest,
                        purpose.value,
                        sorted(operations),
                        now,
                        expires_at,
                    ),
                )
                self._audit(
                    connection,
                    actor_kind="system",
                    actor_id="trusted-console",
                    authority_class="system",
                    action="administrator.trusted_grant.create",
                    result="permitted",
                    now=now,
                    detail={"resource_id": str(url_id), "reason": purpose.value},
                )
        except AdministratorAuthError as error:
            self._audit_failure(
                request_id=request_id,
                action="administrator.trusted_grant.create",
                now=now,
                code=error.code,
            )
            raise
        return TrustedGrantURL(
            url=f"{self._trusted_grant_base_url}#{urlencode({'token': token.value})}",
            expires_at=expires_at,
        )

    def start_authorization(  # noqa: PLR0913
        self,
        purpose: AuthenticationPurpose,
        return_path: str,
        *,
        request_id: str,
        now: datetime,
        session_token: str | None = None,
        trusted_grant_token: str | None = None,
    ) -> AuthorizationStart:
        """Create one server-held state, nonce, and encrypted PKCE verifier."""
        self._require_time(now)
        if purpose is AuthenticationPurpose.RECENT_AUTHENTICATION:
            self._clear_expired_sessions(now)
        if not self._valid_return_path(return_path):
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.start",
                now=now,
                code="invalid_request",
            )
            raise AdministratorAuthError("invalid_request", request_id)
        if (
            session_token is not None
            and not self._valid_generated_secret(session_token)
        ) or (
            trusted_grant_token is not None
            and not self._valid_generated_secret(trusted_grant_token)
        ):
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.start",
                now=now,
                code="invalid_token",
            )
            raise AdministratorAuthError("invalid_token", request_id)
        try:
            if not self._identity_service.available():
                raise IdentityServiceUnavailable
        except IdentityServiceUnavailable as error:
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.start",
                now=now,
                code="temporarily_unavailable",
            )
            raise AdministratorAuthError(
                "temporarily_unavailable", request_id
            ) from error
        state, nonce, verifier = self._secret(), self._secret(), self._secret()
        start_id = self._identity_factory()
        expires_at = now + OIDC_START_LIFETIME
        ciphertext = self._encrypt(verifier.value, start_id)
        try:
            with self._connect() as connection, connection.transaction():
                session_id: uuid.UUID | None = None
                trusted_url_id: uuid.UUID | None = None
                if purpose is AuthenticationPurpose.RECENT_AUTHENTICATION:
                    if session_token is None or trusted_grant_token is not None:
                        raise AdministratorAuthError("invalid_request", request_id)
                    row = self._session_row(
                        connection,
                        session_token,
                        request_id=request_id,
                        for_update=True,
                    )
                    self._check_session_time(row, now, request_id=request_id)
                    session_id = row["id"]
                elif session_token is not None:
                    raise AdministratorAuthError("invalid_request", request_id)
                if trusted_grant_token is not None:
                    trusted = connection.execute(
                        """
                        SELECT id, expires_at, redeemed_at
                        FROM router.trusted_administrator_grant_urls
                        WHERE verifier_digest = %s
                        FOR UPDATE
                        """,
                        (self._digest("trusted-grant", trusted_grant_token),),
                    ).fetchone()
                    if (
                        trusted is None
                        or trusted["redeemed_at"] is not None
                        or trusted["expires_at"] <= now
                    ):
                        raise AdministratorAuthError("invalid_token", request_id)
                    trusted_url_id = trusted["id"]
                connection.execute(
                    """
                    INSERT INTO router.administrator_oidc_starts (
                        id, state_digest, nonce_digest, pkce_verifier_ciphertext,
                        purpose, return_path, session_id, trusted_grant_url_id,
                        exact_redirect_uri, created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        start_id,
                        self._digest("oidc-state", state.value),
                        self._digest("oidc-nonce", nonce.value),
                        ciphertext,
                        purpose.value,
                        return_path,
                        session_id,
                        trusted_url_id,
                        self.configuration.redirect_uri,
                        now,
                        expires_at,
                    ),
                )
                self._audit(
                    connection,
                    actor_kind="system",
                    actor_id="oidc-start",
                    authority_class="system",
                    action="administrator.session.start",
                    result="permitted",
                    now=now,
                    detail={"resource_id": str(start_id), "reason": purpose.value},
                )
        except AdministratorAuthError:
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.start",
                now=now,
            )
            raise
        return AuthorizationStart(
            authorization_url=build_authorization_url(
                self.configuration,
                state=state.value,
                nonce=nonce.value,
                pkce_verifier=verifier.value,
                recent_authentication=(
                    purpose is AuthenticationPurpose.RECENT_AUTHENTICATION
                ),
            ),
            expires_at=expires_at,
        )

    def complete_authorization(
        self,
        code: str,
        state: str,
        *,
        request_id: str,
        now: datetime,
    ) -> SessionResult:
        """Atomically consume callback state and create or refresh one session."""
        self._require_time(now)
        if not 1 <= len(code) <= 4096 or not self._valid_generated_secret(state):
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.complete",
                now=now,
                code="invalid_request",
            )
            raise AdministratorAuthError("invalid_request", request_id)
        trusted_redemption_attempted = False
        try:
            with self._connect() as connection, connection.transaction():
                start = connection.execute(
                    """
                    SELECT * FROM router.administrator_oidc_starts
                    WHERE state_digest = %s
                    FOR UPDATE
                    """,
                    (self._digest("oidc-state", state),),
                ).fetchone()
                if (
                    start is None
                    or start["redeemed_at"] is not None
                    or start["expires_at"] <= now
                    or start["exact_redirect_uri"] != self.configuration.redirect_uri
                ):
                    raise AdministratorAuthError("invalid_token", request_id)
                trusted_redemption_attempted = start["trusted_grant_url_id"] is not None
                verifier = self._decrypt(
                    start["pkce_verifier_ciphertext"],
                    start["id"],
                    request_id=request_id,
                )
                try:
                    response = self._identity_service.exchange_code(
                        code=code,
                        redirect_uri=self.configuration.redirect_uri,
                        pkce_verifier=verifier,
                    )
                except IdentityServiceUnavailable as error:
                    raise AdministratorAuthError(
                        "temporarily_unavailable", request_id
                    ) from error
                try:
                    unverified = jwt.decode(
                        response.id_token,
                        options={"verify_signature": False},
                        algorithms=[self.configuration.signing_algorithm],
                    )
                    nonce = unverified["nonce"]
                except (jwt.PyJWTError, KeyError, TypeError) as error:
                    raise AdministratorAuthError("invalid_token", request_id) from error
                if not isinstance(nonce, str) or not hmac.compare_digest(
                    self._digest("oidc-nonce", nonce), bytes(start["nonce_digest"])
                ):
                    raise AdministratorAuthError("invalid_token", request_id)
                identity = self._token_verifier.verify(
                    response,
                    expected_nonce=nonce,
                    now=now,
                    request_id=request_id,
                )
                if (
                    start["purpose"]
                    == AuthenticationPurpose.RECENT_AUTHENTICATION.value
                    and now - identity.authenticated_at > RECENT_AUTH_LIMIT
                ):
                    raise AdministratorAuthError("recent_auth_required", request_id)
                try:
                    identity_state = self._identity_service.account_state(
                        issuer=identity.issuer, subject=identity.subject, now=now
                    )
                except IdentityServiceUnavailable as error:
                    raise AdministratorAuthError(
                        "temporarily_unavailable", request_id
                    ) from error
                self._require_current_identity_state(identity_state, now, request_id)
                if not identity_state.active:
                    raise AdministratorAuthError("invalid_token", request_id)
                if (
                    not isinstance(response.token_type, str)
                    or response.token_type.casefold() != "bearer"
                    or response.access_token is None
                    or response.refresh_token is None
                    or response.expires_in is None
                ):
                    raise AdministratorAuthError("invalid_token", request_id)
                administrator_id = self._upsert_administrator(
                    connection,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    generation=identity_state.generation,
                )
                session_token: SecretValue | None = None
                csrf_token: SecretValue | None = None
                original_authenticated_at = now
                if start["purpose"] == AuthenticationPurpose.LOGIN.value:
                    if start["trusted_grant_url_id"] is not None:
                        self._redeem_trusted_grant(
                            connection,
                            start["trusted_grant_url_id"],
                            administrator_id,
                            identity.issuer,
                            identity.subject,
                            now,
                            request_id,
                        )
                    session_token, csrf_token = self._secret(), self._secret()
                    session_id = self._identity_factory()
                    absolute_expiry = now + ADMINISTRATOR_ABSOLUTE_LIMIT
                    idle_expiry = now + ADMINISTRATOR_IDLE_LIMIT
                    connection.execute(
                        """
                        INSERT INTO router.administrator_sessions (
                            id, administrator_id, token_digest, csrf_digest,
                            exact_origin, authenticated_at, recent_authenticated_at,
                            account_checked_at, last_used_at, idle_expires_at,
                            absolute_expires_at, identity_generation,
                            provider_access_token_ciphertext,
                            provider_refresh_token_ciphertext,
                            provider_access_expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  %s, %s, %s)
                        """,
                        (
                            session_id,
                            administrator_id,
                            self._digest("session", session_token.value),
                            self._digest("csrf", csrf_token.value),
                            self._exact_origin,
                            now,
                            identity.authenticated_at,
                            identity_state.checked_at,
                            now,
                            idle_expiry,
                            absolute_expiry,
                            identity_state.generation,
                            self._encrypt(response.access_token, session_id),
                            self._encrypt(response.refresh_token, session_id),
                            now + timedelta(seconds=response.expires_in),
                        ),
                    )
                else:
                    session_id = start["session_id"]
                    session = connection.execute(
                        """
                        SELECT session.*, administrator.issuer, administrator.subject,
                               administrator.state AS administrator_state,
                               administrator.identity_generation
                                   AS current_identity_generation
                        FROM router.administrator_sessions AS session
                        JOIN router.administrators AS administrator
                          ON administrator.id = session.administrator_id
                        WHERE session.id = %s
                        FOR UPDATE OF session
                        """,
                        (session_id,),
                    ).fetchone()
                    if session is None:
                        raise AdministratorAuthError("invalid_token", request_id)
                    self._check_session_time(session, now, request_id=request_id)
                    if (
                        session["administrator_id"] != administrator_id
                        or session["identity_generation"] != identity_state.generation
                    ):
                        raise AdministratorAuthError("invalid_token", request_id)
                    absolute_expiry = session["absolute_expires_at"]
                    original_authenticated_at = session["authenticated_at"]
                    idle_expiry = min(now + ADMINISTRATOR_IDLE_LIMIT, absolute_expiry)
                    connection.execute(
                        """
                        UPDATE router.administrator_sessions
                        SET recent_authenticated_at = %s, account_checked_at = %s,
                            last_used_at = %s, idle_expires_at = %s,
                            identity_generation = %s,
                            provider_access_token_ciphertext = %s,
                            provider_refresh_token_ciphertext = %s,
                            provider_access_expires_at = %s
                        WHERE id = %s
                        """,
                        (
                            identity.authenticated_at,
                            identity_state.checked_at,
                            now,
                            idle_expiry,
                            identity_state.generation,
                            self._encrypt(response.access_token, session_id),
                            self._encrypt(response.refresh_token, session_id),
                            now + timedelta(seconds=response.expires_in),
                            session_id,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE router.administrator_oidc_starts
                    SET redeemed_at = %s WHERE id = %s
                    """,
                    (now, start["id"]),
                )
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(identity.issuer, identity.subject),
                    authority_class="global_administrator",
                    action="administrator.session.complete",
                    result="permitted",
                    now=now,
                    detail={"resource_id": str(session_id), "reason": start["purpose"]},
                )
                grants = self._load_grants(
                    connection, identity.issuer, identity.subject, now=now
                )
                return SessionResult(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    grants=tuple(grant.grant_id for grant in grants),
                    authenticated_at=(
                        now
                        if start["purpose"] == AuthenticationPurpose.LOGIN.value
                        else original_authenticated_at
                    ),
                    recent_authentication_at=identity.authenticated_at,
                    account_state_checked_at=identity_state.checked_at,
                    idle_expires_at=idle_expiry,
                    absolute_expires_at=absolute_expiry,
                    return_path=start["return_path"],
                    identity_account_url=self.configuration.account_url,
                )
        except AdministratorAuthError as error:
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.complete",
                now=now,
                code=error.code,
            )
            if trusted_redemption_attempted:
                self._audit_failure(
                    request_id=request_id,
                    action="administrator.trusted_grant.failure",
                    now=now,
                    code=error.code,
                )
            raise

    def authenticate_session(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
        policy: OperationPolicy,
        service_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AdministratorPrincipal:
        """Return one exact effective grant after session and identity-state checks."""
        self._require_time(now)
        self._clear_expired_sessions(now)
        self._require_uuid(service_id, request_id=request_id)
        self._require_uuid(workspace_id, request_id=request_id)
        if workspace_id is not None and service_id is None:
            raise AdministratorAuthError("invalid_request", request_id)
        if policy.authority_path is not AuthorityPath.GLOBAL_ADMINISTRATION:
            raise AdministratorAuthError("insufficient_scope", request_id)
        with self._connect() as connection, connection.transaction():
            session = self._session_row(
                connection, session_token, request_id=request_id, for_update=False
            )
            self._check_session_time(session, now, request_id=request_id)
            refresh = (
                policy.sensitive
                or now - session["account_checked_at"] > ACCOUNT_STATE_LIMIT
            )
        if refresh:
            try:
                self._refresh_token_identity(
                    session_token, request_id=request_id, now=now, always=True
                )
            except AdministratorAuthError:
                if policy.sensitive:
                    self._audit_failure(
                        request_id=request_id, action=policy.operation, now=now
                    )
                raise
        with self._connect() as connection, connection.transaction():
            session = self._session_row(
                connection, session_token, request_id=request_id, for_update=True
            )
            self._check_session_time(session, now, request_id=request_id)
            grant = self._select_grant(
                connection,
                administrator_id=session["administrator_id"],
                operation=policy.operation,
                service_id=service_id,
                workspace_id=workspace_id,
                now=now,
            )
            if grant is None:
                if policy.sensitive:
                    self._audit(
                        connection,
                        actor_kind="administrator",
                        actor_id=self._actor_id(session["issuer"], session["subject"]),
                        authority_class="global_administrator",
                        action=policy.operation,
                        result="denied",
                        now=now,
                        detail={"safe_error_code": "insufficient_scope"},
                    )
                denied_code: str | None = "insufficient_scope"
            else:
                denied_code = None
            recent_at = session["recent_authenticated_at"]
            if (
                grant is not None
                and policy.sensitive
                and (
                    recent_at is None
                    or recent_at > now
                    or now - recent_at > RECENT_AUTH_LIMIT
                )
            ):
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(session["issuer"], session["subject"]),
                    authority_class=self._db_authority(grant["authority_class"]),
                    action=policy.operation,
                    result="denied",
                    now=now,
                    detail={"safe_error_code": "recent_auth_required"},
                )
                denied_code = "recent_auth_required"
            if denied_code is None:
                if grant is None:
                    msg = "The grant decision has no selected grant."
                    raise RuntimeError(msg)
                idle_expiry = min(
                    now + ADMINISTRATOR_IDLE_LIMIT, session["absolute_expires_at"]
                )
                connection.execute(
                    """
                    UPDATE router.administrator_sessions
                    SET last_used_at = %s, idle_expires_at = %s
                    WHERE id = %s
                    """,
                    (now, idle_expiry, session["id"]),
                )
                if policy.sensitive:
                    self._audit(
                        connection,
                        actor_kind="administrator",
                        actor_id=self._actor_id(session["issuer"], session["subject"]),
                        authority_class=self._db_authority(grant["authority_class"]),
                        action=policy.operation,
                        result="permitted",
                        now=now,
                    )
                workspace_ids = frozenset(str(item) for item in grant["workspace_ids"])
                allowed_services = (
                    None
                    if grant["authority_class"] == "global"
                    else frozenset({cast("str", service_id)})
                )
                principal = AdministratorPrincipal(
                    issuer=session["issuer"],
                    subject=session["subject"],
                    authority_class=(
                        AuthorityClass.GLOBAL_ADMINISTRATOR
                        if grant["authority_class"] == "global"
                        else AuthorityClass.SERVICE
                    ),
                    operations=frozenset(grant["operations"]),
                    authenticated_at=session["authenticated_at"],
                    last_activity_at=now,
                    recent_authentication_at=recent_at,
                    account_checked_at=session["account_checked_at"],
                    idle_expires_at=idle_expiry,
                    absolute_expires_at=session["absolute_expires_at"],
                    grant_revision=grant["revision"],
                    allowed_service_ids=allowed_services,
                    allowed_workspace_ids=(workspace_ids or None),
                )
            else:
                principal = None
        if principal is None:
            if denied_code is None:
                msg = "The denied administrator decision has no safe error code."
                raise RuntimeError(msg)
            raise AdministratorAuthError(denied_code, request_id)
        return principal

    def authorize_session(  # noqa: PLR0913
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
        policy: OperationPolicy,
        scope: Scope,
        csrf_token: str | None = None,
        origin: str | None = None,
    ) -> RequestContext:
        """Authorize one administrator route through the stored browser session."""
        proof: BrowserWriteProof | None = None
        if policy.mutation:
            try:
                self._prepare_browser_mutation(
                    session_token,
                    csrf_token or "",
                    origin or "",
                    request_id=request_id,
                    now=now,
                )
            except AdministratorAuthError as error:
                self._audit_failure(
                    request_id=request_id,
                    action=policy.operation,
                    now=now,
                    code=error.code,
                )
                raise
            proof = BrowserWriteProof(
                allowed_origin=self._exact_origin,
                request_origin=origin or "",
                session_csrf_token=csrf_token or "",
                request_csrf_token=csrf_token or "",
            )
        try:
            principal = self.authenticate_session(
                session_token,
                request_id=request_id,
                now=now,
                policy=policy,
                service_id=scope.service_id,
                workspace_id=scope.workspace_id,
            )
            return authorize(
                principal,
                policy,
                scope,
                request_id=request_id,
                now=now,
                browser_write_proof=proof,
            )
        except AdministratorAuthError as error:
            if not policy.sensitive:
                self._audit_failure(
                    request_id=request_id,
                    action=policy.operation,
                    now=now,
                    code=error.code,
                )
            raise

    def get_session(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
    ) -> SessionResult:
        """Read one local session and rotate its browser-readable CSRF value."""
        self._refresh_token_identity(
            session_token, request_id=request_id, now=now, always=False
        )
        csrf_token = self._secret()
        with self._connect() as connection, connection.transaction():
            session = self._session_row(
                connection, session_token, request_id=request_id, for_update=True
            )
            self._check_session_time(session, now, request_id=request_id)
            idle_expiry = min(
                now + ADMINISTRATOR_IDLE_LIMIT, session["absolute_expires_at"]
            )
            connection.execute(
                """
                UPDATE router.administrator_sessions
                SET csrf_digest = %s, last_used_at = %s, idle_expires_at = %s
                WHERE id = %s
                """,
                (
                    self._digest("csrf", csrf_token.value),
                    now,
                    idle_expiry,
                    session["id"],
                ),
            )
            grants = self._load_grants(
                connection, session["issuer"], session["subject"], now=now
            )
            return SessionResult(
                session_token=None,
                csrf_token=csrf_token,
                issuer=session["issuer"],
                subject=session["subject"],
                grants=tuple(grant.grant_id for grant in grants),
                authenticated_at=session["authenticated_at"],
                recent_authentication_at=session["recent_authenticated_at"],
                account_state_checked_at=session["account_checked_at"],
                idle_expires_at=idle_expiry,
                absolute_expires_at=session["absolute_expires_at"],
                return_path="/admin",
                identity_account_url=self.configuration.account_url,
            )

    def create_grant(  # noqa: PLR0913
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        grant_request: GrantRequest,
        idempotency_key: str,
        *,
        request_id: str,
        now: datetime,
    ) -> AdministratorGrant:
        """Create one grant only when one current grant permits the full delegation."""
        self._require_time(now)
        if (
            not _MINIMUM_IDEMPOTENCY_LENGTH
            <= len(idempotency_key)
            <= _MAXIMUM_IDEMPOTENCY_LENGTH
            or grant_request.expected_revision is not None
            or grant_request.issuer != self.configuration.issuer
        ):
            self._audit_failure(
                request_id=request_id,
                action="administrator.grant.create",
                now=now,
                code="invalid_request",
            )
            raise AdministratorAuthError("invalid_request", request_id)
        fingerprint = self._grant_fingerprint(grant_request)
        try:
            self._prepare_browser_mutation(
                session_token, csrf_token, origin, request_id=request_id, now=now
            )
        except AdministratorAuthError:
            self._audit_failure(
                request_id=request_id,
                action="administrator.grant.create",
                now=now,
            )
            raise
        created: AdministratorGrant | None = None
        denied_code: str | None = None
        with self._connect() as connection, connection.transaction():
            session = self._browser_mutation_session(
                connection,
                session_token,
                csrf_token,
                origin,
                request_id=request_id,
                now=now,
            )
            try:
                self._require_recent(session, now, request_id)
            except AdministratorAuthError:
                self._audit_grant_denial(
                    connection,
                    session,
                    now,
                    action="administrator.grant.create",
                    code="recent_auth_required",
                )
                denied_code = "recent_auth_required"
            if denied_code is None:
                try:
                    self._require_uuid(grant_request.service_id, request_id=request_id)
                    for workspace_id in grant_request.workspace_ids:
                        self._require_uuid(workspace_id, request_id=request_id)
                except AdministratorAuthError:
                    self._audit_grant_denial(
                        connection,
                        session,
                        now,
                        action="administrator.grant.create",
                        code="invalid_request",
                    )
                    denied_code = "invalid_request"
            if (
                denied_code is None
                and grant_request.expires_at is not None
                and grant_request.expires_at <= now
            ):
                self._audit_grant_denial(
                    connection,
                    session,
                    now,
                    action="administrator.grant.create",
                    code="invalid_request",
                )
                denied_code = "invalid_request"
            if denied_code is None and not self._grant_scope_exists(
                connection, grant_request
            ):
                self._audit_grant_denial(
                    connection,
                    session,
                    now,
                    action="administrator.grant.create",
                    code="invalid_request",
                )
                denied_code = "invalid_request"
            if denied_code is None and not self._can_delegate(
                connection, session, grant_request, now
            ):
                self._audit_grant_denial(
                    connection, session, now, action="administrator.grant.create"
                )
                denied_code = "insufficient_scope"
            if denied_code is None:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (
                        f"administrator-grant:{session['administrator_id']}:{idempotency_key}",
                    ),
                )
                replay = connection.execute(
                    """
                    SELECT request_fingerprint, grant_id
                    FROM router.administrator_grant_idempotency_bindings
                    WHERE administrator_id = %s AND idempotency_key = %s
                    """,
                    (session["administrator_id"], idempotency_key),
                ).fetchone()
                if replay is not None and not hmac.compare_digest(
                    bytes(replay["request_fingerprint"]), fingerprint
                ):
                    self._audit_grant_denial(
                        connection,
                        session,
                        now,
                        action="administrator.grant.create",
                        code="idempotency_conflict",
                    )
                    denied_code = "idempotency_conflict"
                elif replay is not None:
                    self._touch_session(connection, session, now)
                    created = self._get_grant(
                        connection,
                        replay["grant_id"],
                        request_id=request_id,
                        now=now,
                    )
            if denied_code is not None or created is not None:
                administrator_id = None
            else:
                self._touch_session(connection, session, now)
                administrator_id = self._upsert_administrator(
                    connection,
                    issuer=grant_request.issuer,
                    subject=grant_request.subject,
                    generation=1,
                    preserve_generation=True,
                )
            if administrator_id is not None:
                grant_id = self._identity_factory()
                legacy_workspace = (
                    next(iter(grant_request.workspace_ids))
                    if len(grant_request.workspace_ids) == 1
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO router.administrator_grants (
                        id, administrator_id, authority_class, service_id, workspace_id,
                        workspace_ids, operations, revision, created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                    """,
                    (
                        grant_id,
                        administrator_id,
                        "global"
                        if grant_request.authority_class
                        is AuthorityClass.GLOBAL_ADMINISTRATOR
                        else "service",
                        grant_request.service_id,
                        legacy_workspace,
                        sorted(grant_request.workspace_ids),
                        sorted(grant_request.operations),
                        now,
                        grant_request.expires_at,
                    ),
                )
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(session["issuer"], session["subject"]),
                    authority_class=self._db_authority(
                        self._manager_authority(connection, session, grant_request, now)
                    ),
                    action="administrator.grant.create",
                    result="permitted",
                    now=now,
                    detail={
                        "resource_id": str(grant_id),
                        "reason": grant_request.reason,
                    },
                )
                created = self._get_grant(
                    connection, grant_id, request_id=request_id, now=now
                )
                connection.execute(
                    """
                    INSERT INTO router.administrator_grant_idempotency_bindings (
                        administrator_id, idempotency_key, request_fingerprint,
                        grant_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        session["administrator_id"],
                        idempotency_key,
                        fingerprint,
                        grant_id,
                        now,
                    ),
                )
        if denied_code is not None:
            raise AdministratorAuthError(denied_code, request_id)
        if created is None:
            msg = "The grant creation did not return its stored record."
            raise RuntimeError(msg)
        return created

    def list_grants(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
    ) -> tuple[AdministratorGrant, ...]:
        """List only grants within one current grant-management boundary."""
        self._refresh_token_identity(
            session_token, request_id=request_id, now=now, always=False
        )
        with self._connect() as connection, connection.transaction():
            session = self._session_row(
                connection, session_token, request_id=request_id, for_update=True
            )
            self._check_session_time(session, now, request_id=request_id)
            managers = self._manager_grants(connection, session, now)
            if not managers:
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(session["issuer"], session["subject"]),
                    authority_class="global_administrator",
                    action="administrator.grant.list",
                    result="denied",
                    now=now,
                    detail={"safe_error_code": "insufficient_scope"},
                )
                denied = True
            else:
                self._touch_session(connection, session, now)
                denied = False
            rows = connection.execute(
                """
                SELECT admin_grant.*, administrator.issuer, administrator.subject
                FROM router.administrator_grants AS admin_grant
                JOIN router.administrators AS administrator
                  ON administrator.id = admin_grant.administrator_id
                ORDER BY admin_grant.created_at, admin_grant.id
                """
            ).fetchall()
            grants = tuple(
                self._grant_from_row(row, now=now)
                for row in rows
                if self._row_visible_to_managers(connection, row, managers)
            )
        if denied:
            raise AdministratorAuthError("insufficient_scope", request_id)
        return grants

    def revoke_grant(  # noqa: PLR0913
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        grant_id: str,
        *,
        request_id: str,
        now: datetime,
    ) -> None:
        """Revoke one grant only inside the current delegation boundary."""
        try:
            self._require_uuid(grant_id, request_id=request_id)
            self._prepare_browser_mutation(
                session_token, csrf_token, origin, request_id=request_id, now=now
            )
        except AdministratorAuthError:
            self._audit_failure(
                request_id=request_id,
                action="administrator.grant.revoke",
                now=now,
            )
            raise
        denied_code: str | None = None
        with self._connect() as connection, connection.transaction():
            session = self._browser_mutation_session(
                connection,
                session_token,
                csrf_token,
                origin,
                request_id=request_id,
                now=now,
            )
            try:
                self._require_recent(session, now, request_id)
            except AdministratorAuthError:
                self._audit_grant_denial(
                    connection,
                    session,
                    now,
                    action="administrator.grant.revoke",
                    code="recent_auth_required",
                )
                denied_code = "recent_auth_required"
            row = connection.execute(
                """
                SELECT admin_grant.*, administrator.issuer, administrator.subject
                FROM router.administrator_grants AS admin_grant
                JOIN router.administrators AS administrator
                  ON administrator.id = admin_grant.administrator_id
                WHERE admin_grant.id = %s
                FOR UPDATE OF admin_grant
                """,
                (grant_id,),
            ).fetchone()
            if row is None and denied_code is None:
                self._audit_grant_denial(
                    connection,
                    session,
                    now,
                    action="administrator.grant.revoke",
                    code="not_found",
                )
                denied_code = "not_found"
            if row is None:
                request = None
            else:
                request = GrantRequest(
                    issuer=row["issuer"],
                    subject=row["subject"],
                    authority_class=(
                        AuthorityClass.GLOBAL_ADMINISTRATOR
                        if row["authority_class"] == "global"
                        else AuthorityClass.SERVICE
                    ),
                    operations=frozenset(row["operations"]),
                    service_id=str(row["service_id"]) if row["service_id"] else None,
                    workspace_ids=frozenset(str(item) for item in row["workspace_ids"]),
                    expires_at=row["expires_at"],
                    reason="Revoke the selected grant",
                )
            if (
                denied_code is None
                and request is not None
                and not self._can_delegate(connection, session, request, now)
            ):
                self._audit_grant_denial(
                    connection, session, now, action="administrator.grant.revoke"
                )
                denied_code = "insufficient_scope"
            if denied_code is None and row is not None:
                self._touch_session(connection, session, now)
                if row["revoked_at"] is None:
                    connection.execute(
                        """
                    UPDATE router.administrator_grants
                    SET revoked_at = %s, revision = revision + 1
                    WHERE id = %s
                    """,
                        (now, grant_id),
                    )
            if denied_code is None and request is not None:
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(session["issuer"], session["subject"]),
                    authority_class=self._db_authority(
                        self._manager_authority(connection, session, request, now)
                    ),
                    action="administrator.grant.revoke",
                    result="permitted",
                    now=now,
                    detail={"resource_id": grant_id},
                )
        if denied_code is not None:
            raise AdministratorAuthError(denied_code, request_id)

    @staticmethod
    def _touch_session(
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        now: datetime,
    ) -> None:
        """Extend idle expiry for one successful authenticated activity."""
        idle_expiry = min(
            now + ADMINISTRATOR_IDLE_LIMIT, session["absolute_expires_at"]
        )
        connection.execute(
            """
            UPDATE router.administrator_sessions
            SET last_used_at = %s, idle_expires_at = %s
            WHERE id = %s
            """,
            (now, idle_expiry, session["id"]),
        )

    def logout(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        *,
        request_id: str,
        now: datetime,
    ) -> None:
        """Revoke one local session after exact Origin and CSRF checks."""
        self._require_time(now)
        self._clear_expired_sessions(now)
        try:
            with self._connect() as connection, connection.transaction():
                session = self._browser_mutation_session(
                    connection,
                    session_token,
                    csrf_token,
                    origin,
                    request_id=request_id,
                    now=now,
                )
                connection.execute(
                    """
                    UPDATE router.administrator_sessions
                    SET revoked_at = %s,
                        provider_access_token_ciphertext = NULL,
                        provider_refresh_token_ciphertext = NULL,
                        provider_access_expires_at = NULL
                    WHERE id = %s
                    """,
                    (now, session["id"]),
                )
                self._audit(
                    connection,
                    actor_kind="administrator",
                    actor_id=self._actor_id(session["issuer"], session["subject"]),
                    authority_class="global_administrator",
                    action="administrator.session.logout",
                    result="permitted",
                    now=now,
                    detail={"resource_id": str(session["id"])},
                )
        except AdministratorAuthError as error:
            self._audit_failure(
                request_id=request_id,
                action="administrator.session.logout",
                now=now,
                code=error.code,
            )
            raise

    def _connect(self) -> Connection[dict[str, Any]]:
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def _session_row(
        self,
        connection: Connection[dict[str, Any]],
        token: str,
        *,
        request_id: str,
        for_update: bool,
    ) -> dict[str, Any]:
        if not self._valid_generated_secret(token):
            raise AdministratorAuthError("invalid_token", request_id)
        query = (
            """
            SELECT session.*, administrator.issuer, administrator.subject,
                   administrator.state AS administrator_state,
                   administrator.identity_generation AS current_identity_generation
            FROM router.administrator_sessions AS session
            JOIN router.administrators AS administrator
              ON administrator.id = session.administrator_id
            WHERE session.token_digest = %s
            FOR UPDATE OF session
            """
            if for_update
            else """
            SELECT session.*, administrator.issuer, administrator.subject,
                   administrator.state AS administrator_state,
                   administrator.identity_generation AS current_identity_generation
            FROM router.administrator_sessions AS session
            JOIN router.administrators AS administrator
              ON administrator.id = session.administrator_id
            WHERE session.token_digest = %s
            """
        )
        row = connection.execute(
            query,
            (self._digest("session", token),),
        ).fetchone()
        if row is None:
            raise AdministratorAuthError("invalid_token", request_id)
        return row

    def _browser_mutation_session(
        self,
        connection: Connection[dict[str, Any]],
        session_token: str,
        csrf_token: str,
        origin: str,
        *,
        request_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        if not self._valid_generated_secret(csrf_token) or len(origin) > 2000:
            raise AdministratorAuthError("insufficient_scope", request_id)
        session = self._session_row(
            connection, session_token, request_id=request_id, for_update=True
        )
        self._check_session_time(session, now, request_id=request_id)
        proof = BrowserWriteProof(
            allowed_origin=session["exact_origin"],
            request_origin=origin,
            session_csrf_token=base64.urlsafe_b64encode(
                bytes(session["csrf_digest"])
            ).decode(),
            request_csrf_token=base64.urlsafe_b64encode(
                self._digest("csrf", csrf_token)
            ).decode(),
        )
        if (
            proof.request_origin != self._exact_origin
            or proof.request_origin != proof.allowed_origin
            or not hmac.compare_digest(
                proof.session_csrf_token, proof.request_csrf_token
            )
        ):
            raise AdministratorAuthError("insufficient_scope", request_id)
        return session

    def _prepare_browser_mutation(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        *,
        request_id: str,
        now: datetime,
    ) -> None:
        """Check browser proof before an authoritative identity-state refresh."""
        with self._connect() as connection, connection.transaction():
            self._browser_mutation_session(
                connection,
                session_token,
                csrf_token,
                origin,
                request_id=request_id,
                now=now,
            )
        self._refresh_token_identity(
            session_token, request_id=request_id, now=now, always=True
        )

    def _refresh_token_identity(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
        always: bool,
    ) -> None:
        """Commit disablement or recovery invalidation before a safe failure."""
        self._clear_expired_sessions(now)
        rotation = [False]
        try:
            with self._connect() as connection, connection.transaction():
                session = self._session_row(
                    connection, session_token, request_id=request_id, for_update=True
                )
                self._check_session_time(session, now, request_id=request_id)
                if (
                    not always
                    and now - session["account_checked_at"] <= ACCOUNT_STATE_LIMIT
                ):
                    return
                valid = self._refresh_identity_state(
                    connection, session, now, request_id, rotation
                )
        except Exception:
            if rotation[0]:
                self._revoke_after_rotation(session_token, now)
            raise
        if not valid:
            raise AdministratorAuthError("invalid_token", request_id)

    def _check_session_time(
        self, session: dict[str, Any], now: datetime, *, request_id: str
    ) -> None:
        if (
            session["revoked_at"] is not None
            or session["administrator_state"] != "active"
            or session["identity_generation"] != session["current_identity_generation"]
            or session["authenticated_at"] > now
            or session["last_used_at"] > now
            or session["idle_expires_at"] <= now
            or session["absolute_expires_at"] <= now
            or session["account_checked_at"] > now
            or session["provider_access_token_ciphertext"] is None
            or session["provider_refresh_token_ciphertext"] is None
            or session["provider_access_expires_at"] is None
        ):
            raise AdministratorAuthError("invalid_token", request_id)

    def _refresh_identity_state(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        now: datetime,
        request_id: str,
        rotation: list[bool],
    ) -> bool:
        access_token = self._decrypt(
            session["provider_access_token_ciphertext"],
            session["id"],
            request_id=request_id,
        )
        refresh_token = self._decrypt(
            session["provider_refresh_token_ciphertext"],
            session["id"],
            request_id=request_id,
        )
        try:
            provider = self._identity_service.provider_session_state(
                access_token=access_token,
                refresh_token=refresh_token,
                access_expires_at=session["provider_access_expires_at"],
                now=now,
            )
            rotation[0] = provider.refresh_token != refresh_token
            state = self._identity_service.account_state(
                issuer=session["issuer"], subject=session["subject"], now=now
            )
        except (ProviderSessionInvalid, ProviderSessionRotationFailed):
            self._revoke_session_row(connection, session, now)
            return False
        except IdentityServiceUnavailable as error:
            if rotation[0]:
                self._revoke_session_row(connection, session, now)
                return False
            raise AdministratorAuthError(
                "temporarily_unavailable", request_id
            ) from error
        if provider.checked_at > now or now - provider.checked_at > ACCOUNT_STATE_LIMIT:
            raise AdministratorAuthError("temporarily_unavailable", request_id)
        self._require_current_identity_state(state, now, request_id)
        if (
            not provider.active
            or not state.active
            or state.generation != session["identity_generation"]
        ):
            connection.execute(
                """
                UPDATE router.administrator_sessions
                SET revoked_at = COALESCE(revoked_at, %s),
                    provider_access_token_ciphertext = NULL,
                    provider_refresh_token_ciphertext = NULL,
                    provider_access_expires_at = NULL
                WHERE administrator_id = %s
                """,
                (now, session["administrator_id"]),
            )
            connection.execute(
                """
                UPDATE router.administrators
                SET state = %s, identity_generation = %s
                WHERE id = %s
                """,
                (
                    "active" if state.active else "disabled",
                    state.generation,
                    session["administrator_id"],
                ),
            )
            self._audit(
                connection,
                actor_kind="system",
                actor_id="identity-state-check",
                authority_class="system",
                action="administrator.session.invalidate",
                result="permitted",
                now=now,
                detail={"resource_id": str(session["id"])},
            )
            return False
        connection.execute(
            """
            UPDATE router.administrator_sessions
            SET provider_access_token_ciphertext = %s,
                provider_refresh_token_ciphertext = %s,
                provider_access_expires_at = %s
            WHERE id = %s
            """,
            (
                self._encrypt(provider.access_token, session["id"]),
                self._encrypt(provider.refresh_token, session["id"]),
                provider.access_expires_at,
                session["id"],
            ),
        )
        connection.execute(
            "UPDATE router.administrator_sessions SET account_checked_at = %s WHERE id = %s",
            (state.checked_at, session["id"]),
        )
        session["account_checked_at"] = state.checked_at
        return True

    def _revoke_session_row(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE router.administrator_sessions
            SET revoked_at = COALESCE(revoked_at, %s),
                provider_access_token_ciphertext = NULL,
                provider_refresh_token_ciphertext = NULL,
                provider_access_expires_at = NULL
            WHERE id = %s
            """,
            (now, session["id"]),
        )

    def _revoke_after_rotation(self, session_token: str, now: datetime) -> None:
        """Remove provider secrets after a failure follows remote rotation."""
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE router.administrator_sessions
                SET revoked_at = COALESCE(revoked_at, %s),
                    provider_access_token_ciphertext = NULL,
                    provider_refresh_token_ciphertext = NULL,
                    provider_access_expires_at = NULL
                WHERE token_digest = %s
                """,
                (now, self._digest("session", session_token)),
            )

    def _clear_expired_sessions(self, now: datetime) -> None:
        """Revoke expired sessions and remove their provider secrets."""
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE router.administrator_sessions
                SET revoked_at = COALESCE(revoked_at, %s),
                    provider_access_token_ciphertext = NULL,
                    provider_refresh_token_ciphertext = NULL,
                    provider_access_expires_at = NULL
                WHERE revoked_at IS NULL
                  AND (idle_expires_at <= %s OR absolute_expires_at <= %s)
                """,
                (now, now, now),
            )

    def _require_current_identity_state(
        self, state: Any, now: datetime, request_id: str
    ) -> None:
        if state.checked_at > now or now - state.checked_at > ACCOUNT_STATE_LIMIT:
            raise AdministratorAuthError("temporarily_unavailable", request_id)

    def _require_recent(
        self, session: dict[str, Any], now: datetime, request_id: str
    ) -> None:
        recent = session["recent_authenticated_at"]
        if recent is None or recent > now or now - recent > RECENT_AUTH_LIMIT:
            raise AdministratorAuthError("recent_auth_required", request_id)

    def _can_delegate(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        request: GrantRequest,
        now: datetime,
    ) -> bool:
        for manager in self._manager_grants(connection, session, now):
            if self._manager_permits(connection, manager, request):
                return True
        return False

    def _manager_authority(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        request: GrantRequest,
        now: datetime,
    ) -> str:
        """Return the authority class of the grant that permits delegation."""
        for manager in self._manager_grants(connection, session, now):
            if self._manager_permits(connection, manager, request):
                return cast("str", manager["authority_class"])
        return "global"

    def _manager_permits(
        self,
        connection: Connection[dict[str, Any]],
        manager: dict[str, Any],
        request: GrantRequest,
    ) -> bool:
        if manager["expires_at"] is not None and (
            request.expires_at is None or request.expires_at > manager["expires_at"]
        ):
            return False
        if not ({"grant.manage"} | set(request.operations)) <= set(
            manager["operations"]
        ):
            return False
        if request.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR:
            return bool(manager["authority_class"] == "global")
        if manager["authority_class"] == "global":
            return True
        manager_workspaces = {str(item) for item in manager["workspace_ids"]}
        return self._service_is_descendant(
            connection, request.service_id, str(manager["service_id"])
        ) and (
            not manager_workspaces
            or (
                bool(request.workspace_ids)
                and request.workspace_ids <= manager_workspaces
            )
        )

    def _manager_grants(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        return connection.execute(
            """
            SELECT * FROM router.administrator_grants
            WHERE administrator_id = %s
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > %s)
              AND 'grant.manage' = ANY(operations)
            FOR SHARE
            """,
            (session["administrator_id"], now),
        ).fetchall()

    @staticmethod
    def _grant_scope_exists(
        connection: Connection[dict[str, Any]], request: GrantRequest
    ) -> bool:
        """Require a stored service and exact workspace ownership."""
        if request.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR:
            return True
        service = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM router.services WHERE id = %s) AS found",
            (request.service_id,),
        ).fetchone()
        if not service or not service["found"]:
            return False
        if not request.workspace_ids:
            return True
        workspaces = connection.execute(
            """
            SELECT count(*) AS found
            FROM router.workspaces
            WHERE service_id = %s AND id = ANY(%s::uuid[])
            """,
            (request.service_id, sorted(request.workspace_ids)),
        ).fetchone()
        return bool(workspaces and workspaces["found"] == len(request.workspace_ids))

    def _service_is_descendant(
        self,
        connection: Connection[dict[str, Any]],
        candidate: str | None,
        ancestor: str,
    ) -> bool:
        if candidate is None:
            return False
        row = connection.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_service_id FROM router.services WHERE id = %s
              UNION ALL
                SELECT parent.id, parent.parent_service_id
                FROM router.services AS parent
                JOIN ancestors AS child ON child.parent_service_id = parent.id
            )
            SELECT EXISTS (SELECT 1 FROM ancestors WHERE id = %s) AS found
            """,
            (candidate, ancestor),
        ).fetchone()
        return bool(row and row["found"])

    def _row_visible_to_managers(
        self,
        connection: Connection[dict[str, Any]],
        row: dict[str, Any],
        managers: list[dict[str, Any]],
    ) -> bool:
        for manager in managers:
            if manager["authority_class"] == "global":
                return True
            if row["authority_class"] == "global":
                continue
            if self._service_is_descendant(
                connection, str(row["service_id"]), str(manager["service_id"])
            ) and self._workspace_scope_contains(
                manager["workspace_ids"], row["workspace_ids"]
            ):
                return True
        return False

    def _select_grant(  # noqa: PLR0913
        self,
        connection: Connection[dict[str, Any]],
        *,
        administrator_id: uuid.UUID,
        operation: str,
        service_id: str | None,
        workspace_id: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            """
            SELECT * FROM router.administrator_grants
            WHERE administrator_id = %s AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > %s)
              AND %s = ANY(operations)
            ORDER BY authority_class = 'service' DESC,
                     cardinality(workspace_ids) > 0 DESC,
                     created_at, id
            FOR SHARE
            """,
            (administrator_id, now, operation),
        ).fetchall()
        for row in rows:
            if row["authority_class"] == "global":
                return row
            if service_id is None or not self._service_is_descendant(
                connection, service_id, str(row["service_id"])
            ):
                continue
            if not row["workspace_ids"]:
                return row
            if (
                workspace_id is not None
                and uuid.UUID(workspace_id) in row["workspace_ids"]
            ):
                return row
        return None

    @staticmethod
    def _workspace_scope_contains(
        manager_workspace_ids: list[uuid.UUID],
        requested_workspace_ids: list[uuid.UUID],
    ) -> bool:
        """Keep workspace-restricted authority from becoming service authority."""
        manager = set(manager_workspace_ids)
        requested = set(requested_workspace_ids)
        return not manager or (bool(requested) and requested <= manager)

    def _redeem_trusted_grant(  # noqa: PLR0913
        self,
        connection: Connection[dict[str, Any]],
        url_id: uuid.UUID,
        administrator_id: uuid.UUID,
        issuer: str,
        subject: str,
        now: datetime,
        request_id: str,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("administrator-trusted-grant",),
        )
        row = connection.execute(
            """
            SELECT * FROM router.trusted_administrator_grant_urls
            WHERE id = %s FOR UPDATE
            """,
            (url_id,),
        ).fetchone()
        if row is None or row["redeemed_at"] is not None or row["expires_at"] <= now:
            raise AdministratorAuthError("invalid_token", request_id)
        if self._eligible_global_grant_exists(connection, now):
            raise AdministratorAuthError("insufficient_scope", request_id)
        grant_id = self._identity_factory()
        connection.execute(
            """
            INSERT INTO router.administrator_grants (
                id, administrator_id, authority_class, operations, created_at,
                workspace_ids
            ) VALUES (%s, %s, 'global', %s, %s, '{}')
            """,
            (grant_id, administrator_id, row["operations"], now),
        )
        connection.execute(
            """
            UPDATE router.trusted_administrator_grant_urls
            SET redeemed_at = %s, redeemed_administrator_id = %s
            WHERE id = %s
            """,
            (now, administrator_id, url_id),
        )
        self._audit(
            connection,
            actor_kind="administrator",
            actor_id=self._actor_id(issuer, subject),
            authority_class="global_administrator",
            action="administrator.trusted_grant.redeem",
            result="permitted",
            now=now,
            detail={"resource_id": str(grant_id), "reason": row["purpose"]},
        )
        self._audit(
            connection,
            actor_kind="administrator",
            actor_id=self._actor_id(issuer, subject),
            authority_class="global_administrator",
            action="administrator.trusted_grant.success",
            result="permitted",
            now=now,
            detail={"resource_id": str(grant_id), "reason": row["purpose"]},
        )

    def _eligible_global_grant_exists(
        self,
        connection: Connection[dict[str, Any]],
        now: datetime,
    ) -> bool:
        row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM router.administrator_grants AS admin_grant
                JOIN router.administrators AS administrator
                  ON administrator.id = admin_grant.administrator_id
                WHERE admin_grant.authority_class = 'global'
                  AND admin_grant.revoked_at IS NULL
                  AND (admin_grant.expires_at IS NULL OR admin_grant.expires_at > %s)
                  AND administrator.state = 'active'
                  AND 'grant.manage' = ANY(admin_grant.operations)
            ) AS found
            """,
            (now,),
        ).fetchone()
        return bool(row and row["found"])

    def _upsert_administrator(  # noqa: PLR0913
        self,
        connection: Connection[dict[str, Any]],
        *,
        issuer: str,
        subject: str,
        generation: int,
        preserve_generation: bool = False,
    ) -> uuid.UUID:
        administrator_id = self._identity_factory()
        if preserve_generation:
            row = connection.execute(
                """
                INSERT INTO router.administrators (
                    id, issuer, subject, state, identity_generation
                ) VALUES (%s, %s, %s, 'active', %s)
                ON CONFLICT (issuer, subject) DO UPDATE
                SET issuer = EXCLUDED.issuer
                RETURNING id
                """,
                (administrator_id, issuer, subject, generation),
            ).fetchone()
        else:
            row = connection.execute(
                """
                INSERT INTO router.administrators (
                    id, issuer, subject, state, identity_generation
                ) VALUES (%s, %s, %s, 'active', %s)
                ON CONFLICT (issuer, subject) DO UPDATE
                SET state = 'active', identity_generation = EXCLUDED.identity_generation
                RETURNING id
                """,
                (administrator_id, issuer, subject, generation),
            ).fetchone()
        if row is None:
            msg = "The administrator upsert did not return an identity."
            raise RuntimeError(msg)
        return cast("uuid.UUID", row["id"])

    def _load_grants(
        self,
        connection: Connection[dict[str, Any]],
        issuer: str,
        subject: str,
        *,
        now: datetime,
    ) -> tuple[AdministratorGrant, ...]:
        rows = connection.execute(
            """
            SELECT admin_grant.*, administrator.issuer, administrator.subject
            FROM router.administrator_grants AS admin_grant
            JOIN router.administrators AS administrator
              ON administrator.id = admin_grant.administrator_id
            WHERE administrator.issuer = %s AND administrator.subject = %s
              AND admin_grant.revoked_at IS NULL
              AND (admin_grant.expires_at IS NULL OR admin_grant.expires_at > %s)
            ORDER BY admin_grant.created_at, admin_grant.id
            """,
            (issuer, subject, now),
        ).fetchall()
        return tuple(self._grant_from_row(row, now=now) for row in rows)

    def _get_grant(
        self,
        connection: Connection[dict[str, Any]],
        grant_id: uuid.UUID,
        *,
        request_id: str,
        now: datetime,
    ) -> AdministratorGrant:
        row = connection.execute(
            """
            SELECT admin_grant.*, administrator.issuer, administrator.subject
            FROM router.administrator_grants AS admin_grant
            JOIN router.administrators AS administrator
              ON administrator.id = admin_grant.administrator_id
            WHERE admin_grant.id = %s
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            raise AdministratorAuthError("not_found", request_id)
        return self._grant_from_row(row, now=now)

    @staticmethod
    def _grant_from_row(row: dict[str, Any], *, now: datetime) -> AdministratorGrant:
        return AdministratorGrant(
            grant_id=str(row["id"]),
            issuer=row["issuer"],
            subject=row["subject"],
            authority_class=(
                AuthorityClass.GLOBAL_ADMINISTRATOR
                if row["authority_class"] == "global"
                else AuthorityClass.SERVICE
            ),
            operations=frozenset(row["operations"]),
            service_id=str(row["service_id"]) if row["service_id"] else None,
            workspace_ids=frozenset(str(item) for item in row["workspace_ids"]),
            state=(
                GrantState.REVOKED
                if row["revoked_at"] is not None
                else GrantState.EXPIRED
                if row["expires_at"] is not None and row["expires_at"] <= now
                else GrantState.ACTIVE
            ),
            revision=str(row["revision"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def _audit_grant_denial(
        self,
        connection: Connection[dict[str, Any]],
        session: dict[str, Any],
        now: datetime,
        *,
        action: str,
        code: str = "insufficient_scope",
    ) -> None:
        self._audit(
            connection,
            actor_kind="administrator",
            actor_id=self._actor_id(session["issuer"], session["subject"]),
            authority_class="global_administrator",
            action=action,
            result="denied",
            now=now,
            detail={"safe_error_code": code},
        )

    def _audit_failure(
        self,
        *,
        request_id: str,
        action: str,
        now: datetime,
        code: str = "authentication_failed",
    ) -> None:
        with self._connect() as connection, connection.transaction():
            self._audit(
                connection,
                actor_kind="system",
                actor_id="unauthenticated-administrator",
                authority_class="system",
                action=action,
                result="denied",
                now=now,
                detail={"safe_error_code": code},
            )

    def _audit(  # noqa: PLR0913
        self,
        connection: Connection[dict[str, Any]],
        *,
        actor_kind: str,
        actor_id: str,
        authority_class: str,
        action: str,
        result: str,
        now: datetime,
        detail: dict[str, str] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO router.audit_events (
                event_id, audit_class, actor_kind, actor_id, authority_class,
                action, permission_result, safe_details, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                self._identity_factory(),
                "security"
                if action.startswith("administrator.session")
                or action.startswith("administrator.trusted")
                else "global_administration",
                actor_kind,
                actor_id,
                authority_class,
                action,
                result,
                json.dumps(detail or {}, sort_keys=True),
                now,
            ),
        )

    def _secret(self) -> SecretValue:
        return SecretValue(
            base64.urlsafe_b64encode(self._random_bytes(32)).rstrip(b"=").decode()
        )

    def _digest(self, domain: str, value: str) -> bytes:
        return hmac.digest(
            self._digest_key,
            f"llmrouter-admin-{domain}-v1\x00{value}".encode(),
            "sha256",
        )

    def _encrypt(self, value: str, start_id: uuid.UUID) -> bytes:
        nonce = self._random_bytes(24)
        return nonce + crypto_aead_xchacha20poly1305_ietf_encrypt(
            value.encode(),
            _ENCRYPTION_DOMAIN + start_id.bytes,
            nonce,
            self._encryption_key,
        )

    def _decrypt(
        self,
        ciphertext: bytes,
        start_id: uuid.UUID,
        *,
        request_id: str,
    ) -> str:
        try:
            return crypto_aead_xchacha20poly1305_ietf_decrypt(
                bytes(ciphertext)[24:],
                _ENCRYPTION_DOMAIN + start_id.bytes,
                bytes(ciphertext)[:24],
                self._encryption_key,
            ).decode()
        except (CryptoError, UnicodeDecodeError) as error:
            raise AdministratorAuthError("invalid_token", request_id) from error

    @staticmethod
    def _grant_fingerprint(request: GrantRequest) -> bytes:
        canonical = json.dumps(
            {
                "authority_class": (
                    "global"
                    if request.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
                    else "service"
                ),
                "expires_at": (
                    request.expires_at.isoformat()
                    if request.expires_at is not None
                    else None
                ),
                "issuer": request.issuer,
                "operations": sorted(request.operations),
                "reason": request.reason,
                "service_id": request.service_id,
                "subject": request.subject,
                "workspace_ids": sorted(request.workspace_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).digest()

    @staticmethod
    def _require_uuid(value: str | None, *, request_id: str) -> None:
        """Reject malformed public identifiers with one safe contract error."""
        if value is None:
            return
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as error:
            raise AdministratorAuthError("invalid_request", request_id) from error
        if str(parsed) != value:
            raise AdministratorAuthError("invalid_request", request_id)

    @staticmethod
    def _actor_id(issuer: str, subject: str) -> str:
        return f"{len(issuer)}:{issuer}{subject}"

    @staticmethod
    def _db_authority(value: str) -> str:
        return "global_administrator" if value == "global" else "service"

    @staticmethod
    def _valid_return_path(value: str) -> bool:
        return (
            bool(value)
            and value.startswith("/")
            and not value.startswith("//")
            and "\\" not in value
            and len(value) <= 2000
            and _RETURN_PATH.fullmatch(value) is not None
        )

    @staticmethod
    def _valid_generated_secret(value: str) -> bool:
        try:
            SecretValue(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _require_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "The operation time must include a time zone."
            raise ValueError(msg)

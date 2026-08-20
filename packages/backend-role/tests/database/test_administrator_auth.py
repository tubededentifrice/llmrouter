"""Pocket ID callback, session, grant, and browser-boundary tests."""
# ruff: noqa: D102, D103, D107, PLR2004, PT018, S105, S106

from __future__ import annotations

import base64
import concurrent.futures
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from llmrouter_backend.admin_auth import (
    AdministratorAuthError,
    AdministratorAuthRepository,
    AuthenticationPurpose,
    GrantRequest,
    IdentityServiceUnavailable,
    OIDCConfiguration,
    OIDCTokenResponse,
    OIDCTokenVerifier,
    ProviderSessionInvalid,
    ProviderSessionRotationFailed,
    ProviderSessionState,
    TrustedGrantPurpose,
    administrator_session_cookie,
)
from llmrouter_backend.authority import (
    AdministratorPrincipal,
    Audience,
    AuthorityClass,
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    ScopeKind,
    ServicePrincipal,
)
from llmrouter_backend.database import migrate

from .helpers import OTHER_WORKSPACE_ID, SERVICE_ID, WORKSPACE_ID, seed_scope

if TYPE_CHECKING:
    from Crypto.PublicKey.RSA import RsaKey


def _encode_id_token(
    claims: dict[str, object], key: RsaKey, *, algorithm: str = "RS256"
) -> str:
    """Create one deterministic RS256 test token."""
    header = {"alg": algorithm, "kid": "identity-key", "typ": "JWT"}

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


NOW = datetime(2026, 8, 13, 13, tzinfo=UTC)
ISSUER = "https://identity.example.test"
CLIENT_ID = "llm-router"
ORIGIN = "https://admin.example.test"
REDIRECT = f"{ORIGIN}/v1/admin/oidc/callback"
DIGEST_KEY = bytes(range(32))
ENCRYPTION_KEY = bytes(reversed(range(32)))


class FakeIdentityService:
    """Deterministic authoritative identity-service boundary."""

    def __init__(self, private_key: RsaKey) -> None:
        self.private_key = private_key
        self.is_available = True
        self.nonce = ""
        self.subject = "person-1"
        self.active = True
        self.token_type = "id-token"
        self.reject_provider_session = False
        self.rotate_provider_session = False
        self.fail_after_rotation = False
        self.provider_calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        if not self.is_available:
            raise IdentityServiceUnavailable
        return True

    def exchange_code(
        self, *, code: str, redirect_uri: str, pkce_verifier: str
    ) -> OIDCTokenResponse:
        if not self.is_available:
            raise IdentityServiceUnavailable
        assert code and redirect_uri == REDIRECT and len(pkce_verifier) == 43
        token = _encode_id_token(
            {
                "iss": ISSUER,
                "sub": self.subject,
                "aud": CLIENT_ID,
                "azp": CLIENT_ID,
                "type": self.token_type,
                "nonce": self.nonce,
                "iat": int(NOW.timestamp()),
                "exp": int((NOW + timedelta(minutes=5)).timestamp()),
                "auth_time": int(NOW.timestamp()),
            },
            self.private_key,
        )
        return OIDCTokenResponse(
            id_token=token,
            token_type="Bearer",
            access_token="provider-access-token",
            refresh_token="provider-refresh-token",
            expires_in=300,
        )

    def provider_session_state(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        now: datetime,
    ) -> ProviderSessionState:
        self.provider_calls.append((access_token, refresh_token))
        if not self.is_available:
            raise IdentityServiceUnavailable
        if self.reject_provider_session:
            raise ProviderSessionInvalid
        if self.fail_after_rotation:
            raise ProviderSessionRotationFailed
        return ProviderSessionState(
            active=self.active,
            access_token=(
                "rotated-access" if self.rotate_provider_session else access_token
            ),
            refresh_token=(
                "rotated-refresh" if self.rotate_provider_session else refresh_token
            ),
            access_expires_at=access_expires_at,
            checked_at=now,
        )


def test_callback_checks_provider_session_and_stores_rotated_tokens(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Check the provider before creation and store its rotated token pair."""
    repository, identity = auth_repository
    identity.rotate_provider_session = True
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    assert identity.provider_calls == [
        ("provider-access-token", "provider-refresh-token")
    ]

    repository.authenticate_session(
        session,
        request_id="rotated-provider-token-check",
        now=NOW + timedelta(minutes=6),
        policy=_policy("health.read"),
    )
    assert identity.provider_calls[1] == ("rotated-access", "rotated-refresh")


def test_callback_rejects_an_inactive_provider_session(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Do not create a local session for an inactive provider session."""
    repository, identity = auth_repository
    state, _ = _start(repository, identity)
    identity.active = False
    with pytest.raises(AdministratorAuthError) as rejected:
        repository.complete_authorization(
            "code", state, request_id="inactive-provider-callback", now=NOW
        )
    assert rejected.value.code == "invalid_token"
    assert identity.provider_calls == [
        ("provider-access-token", "provider-refresh-token")
    ]


@pytest.fixture
def auth_repository(
    database_url: str,
) -> tuple[AdministratorAuthRepository, FakeIdentityService]:
    """Create the schema and one exact confidential identity client."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    private_key = RSA.generate(2048)
    identity = FakeIdentityService(private_key)
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        authorization_endpoint=f"{ISSUER}/authorize?fixed=1",
        redirect_uri=REDIRECT,
        account_url=f"{ISSUER}/account",
        signing_algorithm="RS256",
    )
    repository = AdministratorAuthRepository(
        database_url,
        configuration=configuration,
        identity_service=identity,
        token_verifier=OIDCTokenVerifier(
            configuration, {"identity-key": private_key.public_key().export_key()}
        ),
        digest_key=DIGEST_KEY,
        encryption_key=ENCRYPTION_KEY,
        exact_origin=ORIGIN,
        trusted_grant_base_url=f"{ORIGIN}/trusted-grant",
    )
    return repository, identity


def _start(
    repository: AdministratorAuthRepository,
    identity: FakeIdentityService,
    *,
    trusted_token: str | None = None,
) -> tuple[str, str]:
    start = repository.start_authorization(
        AuthenticationPurpose.LOGIN,
        "/admin",
        request_id="start-request",
        now=NOW,
        trusted_grant_token=trusted_token,
    )
    query = parse_qs(urlsplit(start.authorization_url).query)
    assert query["fixed"] == ["1"]
    assert query["code_challenge_method"] == ["S256"]
    identity.nonce = query["nonce"][0]
    return query["state"][0], query["nonce"][0]


def _bootstrap(
    repository: AdministratorAuthRepository,
    identity: FakeIdentityService,
    operations: frozenset[str],
) -> tuple[str, str]:
    trusted = repository.create_trusted_grant_url(
        TrustedGrantPurpose.INITIAL,
        operations,
        request_id="trusted-request",
        now=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert urlsplit(trusted.url).query == ""
    assert urlsplit(trusted.url).fragment.startswith("token=")
    token = parse_qs(urlsplit(trusted.url).fragment)["token"][0]
    state, _ = _start(repository, identity, trusted_token=token)
    session = repository.complete_authorization(
        "code", state, request_id="callback-request", now=NOW
    )
    assert session.session_token is not None and session.csrf_token is not None
    return session.session_token.value, session.csrf_token.value


def _policy(operation: str, *, sensitive: bool = False) -> OperationPolicy:
    return OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        principal_kinds=frozenset({PrincipalKind.ADMINISTRATOR}),
        scope_kind=ScopeKind.GLOBAL,
        sensitive=sensitive,
        mutation=sensitive,
    )


def test_oidc_configuration_rejects_unsupported_signing_algorithm() -> None:
    with pytest.raises(ValueError, match="must be RS256"):
        OIDCConfiguration(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            authorization_endpoint=f"{ISSUER}/authorize",
            redirect_uri=REDIRECT,
            account_url=f"{ISSUER}/account",
            signing_algorithm="ES256",
        )
    with pytest.raises(ValueError, match="reserved request parameter"):
        OIDCConfiguration(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            authorization_endpoint=f"{ISSUER}/authorize?client_id=attacker",
            redirect_uri=REDIRECT,
            account_url=f"{ISSUER}/account",
            signing_algorithm="RS256",
        )


def test_grant_request_rejects_more_than_contract_workspace_limit() -> None:
    with pytest.raises(ValueError, match="no more than 1000 workspaces"):
        GrantRequest(
            issuer=ISSUER,
            subject="person-2",
            authority_class=AuthorityClass.SERVICE,
            operations=frozenset({"health.read"}),
            reason="Test the workspace contract limit",
            service_id=SERVICE_ID,
            workspace_ids=frozenset(str(index) for index in range(1001)),
        )


def test_trusted_grant_url_requires_exact_structural_origin(
    database_url: str,
) -> None:
    private_key = RSA.generate(2048)
    identity = FakeIdentityService(private_key)
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        authorization_endpoint=f"{ISSUER}/authorize",
        redirect_uri=REDIRECT,
        account_url=f"{ISSUER}/account",
        signing_algorithm="RS256",
    )
    verifier = OIDCTokenVerifier(
        configuration, {"identity-key": private_key.public_key().export_key()}
    )
    for unsafe_url in (
        "https://admin.example.test.evil.test/trusted-grant",
        "https://person@admin.example.test/trusted-grant",
        "https://admin.example.test//evil.test/trusted-grant",
        "https://admin.example.test/trusted-grant?token=attacker",
        "https://admin.example.test/trusted-grant#secret",
    ):
        with pytest.raises(ValueError, match="exact administrator origin"):
            AdministratorAuthRepository(
                database_url,
                configuration=configuration,
                identity_service=identity,
                token_verifier=verifier,
                digest_key=DIGEST_KEY,
                encryption_key=ENCRYPTION_KEY,
                exact_origin=ORIGIN,
                trusted_grant_base_url=unsafe_url,
            )


def test_repository_keys_and_identity_signing_keys_are_separate_and_safe(
    database_url: str,
) -> None:
    """Reject key reuse, private verification keys, and small RSA keys."""
    private_key = RSA.generate(2048)
    identity = FakeIdentityService(private_key)
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        authorization_endpoint=f"{ISSUER}/authorize",
        redirect_uri=REDIRECT,
        account_url=f"{ISSUER}/account",
        signing_algorithm="RS256",
    )
    with pytest.raises(ValueError, match="must be different"):
        AdministratorAuthRepository(
            database_url,
            configuration=configuration,
            identity_service=identity,
            token_verifier=OIDCTokenVerifier(
                configuration,
                {"identity-key": private_key.public_key().export_key()},
            ),
            digest_key=DIGEST_KEY,
            encryption_key=DIGEST_KEY,
            exact_origin=ORIGIN,
            trusted_grant_base_url=f"{ORIGIN}/trusted-grant",
        )
    with pytest.raises(ValueError, match="must be public RSA keys"):
        OIDCTokenVerifier(configuration, {"identity-key": private_key.export_key()})
    small_key = RSA.generate(1024)  # noqa: S505 - This key tests rejection.
    with pytest.raises(ValueError, match="2048 bits or more"):
        OIDCTokenVerifier(
            configuration, {"identity-key": small_key.public_key().export_key()}
        )


def test_cookie_rejects_header_injection() -> None:
    """Accept only generated session-token syntax in a cookie header."""
    with pytest.raises(ValueError, match="generated token"):
        administrator_session_cookie("token\r\nDomain=evil.example")


def test_invalid_trusted_grant_creation_has_exact_failure_audit(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, _ = auth_repository
    with pytest.raises(AdministratorAuthError) as invalid:
        repository.create_trusted_grant_url(
            TrustedGrantPurpose.INITIAL,
            frozenset(),
            request_id="invalid-trusted",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
    assert invalid.value.code == "invalid_request"
    with psycopg.connect(database_url) as connection:
        event = connection.execute(
            """
            SELECT permission_result, safe_details ->> 'safe_error_code'
            FROM router.audit_events
            WHERE action = 'administrator.trusted_grant.create'
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1
            """
        ).fetchone()
    assert event == ("denied", "invalid_request")


def test_callback_is_one_use_and_identity_alone_grants_no_authority(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    state, _ = _start(repository, identity)
    session = repository.complete_authorization(
        "code", state, request_id="callback", now=NOW
    )
    assert session.grants == ()
    assert session.session_token is not None
    with pytest.raises(AdministratorAuthError) as repeated:
        repository.complete_authorization(
            "code", state, request_id="callback-repeat", now=NOW
        )
    assert repeated.value.code == "invalid_token"
    with pytest.raises(AdministratorAuthError) as no_grant:
        repository.authenticate_session(
            session.session_token.value,
            request_id="authority",
            now=NOW,
            policy=_policy("health.read"),
        )
    assert no_grant.value.code == "insufficient_scope"


def test_service_token_cannot_enter_administrator_authentication_path() -> None:
    """Keep the service token type outside the administrator principal type."""
    token = ServicePrincipal(
        issuer="https://router.example.test",
        token_id="token-1",
        audience=Audience.ACCOUNTING,
        service_id=SERVICE_ID,
        operations=frozenset({"accounting.read"}),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        credential_generation=1,
    )
    assert not isinstance(token, AdministratorPrincipal)
    assert token.authority_path is AuthorityPath.MACHINE


def test_machine_token_value_cannot_authenticate_an_administrator_repository(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Reject a machine bearer value through the local session digest path."""
    repository, _ = auth_repository
    with pytest.raises(AdministratorAuthError) as error:
        repository.authenticate_session(
            "machine-access-token-value",
            request_id="machine-confusion",
            now=NOW,
            policy=_policy("health.read"),
        )
    assert error.value.code == "invalid_token"


def test_session_read_rotates_and_returns_the_csrf_token(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Let the browser recover CSRF state without storing a raw server value."""
    repository, identity = auth_repository
    session, prior_csrf = _bootstrap(repository, identity, frozenset({"health.read"}))
    current = repository.get_session(
        session, request_id="get-session", now=NOW + timedelta(minutes=1)
    )
    assert current.session_token is None
    assert current.csrf_token is not None
    assert current.csrf_token.value != prior_csrf
    with pytest.raises(AdministratorAuthError):
        repository.logout(
            session,
            prior_csrf,
            ORIGIN,
            request_id="old-csrf",
            now=NOW + timedelta(minutes=1),
        )


def test_token_confusion_and_exact_oidc_claims_fail_closed(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    state, _ = _start(repository, identity)
    identity.token_type = "access-token"
    with pytest.raises(AdministratorAuthError) as error:
        repository.complete_authorization(
            "code", state, request_id="wrong-use", now=NOW
        )
    assert error.value.code == "invalid_token"


def test_pocket_id_standard_token_shapes_are_accepted(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Accept Pocket ID case, audience, and NumericDate forms."""
    repository, identity = auth_repository
    state, nonce = _start(repository, identity)
    token = _encode_id_token(
        {
            "iss": ISSUER,
            "sub": identity.subject,
            "aud": [CLIENT_ID],
            "azp": CLIENT_ID,
            "type": "id-token",
            "nonce": nonce,
            "iat": NOW.timestamp(),
            "exp": (NOW + timedelta(minutes=5)).timestamp(),
            "auth_time": NOW.timestamp(),
        },
        identity.private_key,
    )
    identity.exchange_code = lambda **_: OIDCTokenResponse(  # type: ignore[method-assign]
        id_token=token,
        token_type="bearer",
        access_token="provider-access-token",
        refresh_token="provider-refresh-token",
        expires_in=300,
    )

    session = repository.complete_authorization(
        "code", state, request_id="pocket-shape", now=NOW
    )

    assert session.session_token is not None


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://wrong.example.test"),
        ("aud", "other-client"),
        ("azp", "other-client"),
        ("nonce", "wrong-nonce"),
        ("type", "access-token"),
    ],
)
def test_exact_identity_claim_matrix_rejects_confusion(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
    claim: str,
    value: str,
) -> None:
    """Reject a changed issuer, client, nonce, or Pocket ID token use."""
    repository, identity = auth_repository
    state, nonce = _start(repository, identity)
    claims = {
        "iss": ISSUER,
        "sub": identity.subject,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "type": "id-token",
        "nonce": nonce,
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        "auth_time": int(NOW.timestamp()),
    }
    claims[claim] = value
    identity.exchange_code = lambda **_: OIDCTokenResponse(  # type: ignore[method-assign]
        id_token=_encode_id_token(claims, identity.private_key),
        token_type="Bearer",
    )
    with pytest.raises(AdministratorAuthError) as error:
        repository.complete_authorization("code", state, request_id="claims", now=NOW)
    assert error.value.code == "invalid_token"


def test_identity_token_rejects_wrong_algorithm_signature_and_response_type(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    for algorithm, key, token_type in (
        ("HS256", identity.private_key, "Bearer"),
        ("RS256", RSA.generate(2048), "Bearer"),
        ("RS256", identity.private_key, "MAC"),
    ):
        state, nonce = _start(repository, identity)
        token = _encode_id_token(
            {
                "iss": ISSUER,
                "sub": identity.subject,
                "aud": CLIENT_ID,
                "type": "id-token",
                "nonce": nonce,
                "iat": int(NOW.timestamp()),
                "exp": int((NOW + timedelta(minutes=5)).timestamp()),
                "auth_time": int(NOW.timestamp()),
            },
            key,
            algorithm=algorithm,
        )
        identity.exchange_code = (  # type: ignore[method-assign]
            lambda *, id_token=token, response_type=token_type, **_: OIDCTokenResponse(
                id_token=id_token, token_type=response_type
            )
        )
        with pytest.raises(AdministratorAuthError) as error:
            repository.complete_authorization(
                "code", state, request_id="token-crypto", now=NOW
            )
        assert error.value.code == "invalid_token"


def test_recent_auth_start_forces_reauthentication_and_checks_session(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Send max_age zero and refresh only the same current human session."""
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    start = repository.start_authorization(
        AuthenticationPurpose.RECENT_AUTHENTICATION,
        "/admin",
        request_id="recent-start",
        now=NOW + timedelta(minutes=1),
        session_token=session,
    )
    query = parse_qs(urlsplit(start.authorization_url).query)
    assert query["max_age"] == ["0"]
    assert query["prompt"] == ["login"]
    assert query["redirect_uri"] == [REDIRECT]
    identity.nonce = query["nonce"][0]
    refreshed = repository.complete_authorization(
        "recent-code",
        query["state"][0],
        request_id="recent-complete",
        now=NOW + timedelta(minutes=1),
    )
    assert refreshed.session_token is None and refreshed.csrf_token is None
    assert refreshed.authenticated_at == NOW
    assert refreshed.recent_authentication_at == NOW


def test_trusted_url_and_callback_are_atomic_under_concurrency(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    trusted = repository.create_trusted_grant_url(
        TrustedGrantPurpose.INITIAL,
        frozenset({"grant.manage", "health.read"}),
        request_id="trusted",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    token = parse_qs(urlsplit(trusted.url).fragment)["token"][0]
    state, _ = _start(repository, identity, trusted_token=token)

    def complete(index: int) -> str:
        try:
            repository.complete_authorization(
                "code", state, request_id=f"callback-{index}", now=NOW
            )
        except AdministratorAuthError as error:
            return error.code
        return "created"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(complete, range(2)))
    assert sorted(results) == ["created", "invalid_token"]


def test_two_distinct_starts_can_redeem_one_trusted_url_only_once(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Serialize two callback states that refer to one trusted grant verifier."""
    repository, identity = auth_repository
    trusted = repository.create_trusted_grant_url(
        TrustedGrantPurpose.INITIAL,
        frozenset({"grant.manage", "health.read"}),
        request_id="trusted-two-starts",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    token = parse_qs(urlsplit(trusted.url).fragment)["token"][0]
    first_state, first_nonce = _start(repository, identity, trusted_token=token)
    second_state, second_nonce = _start(repository, identity, trusted_token=token)
    identity.nonce = first_nonce
    first = repository.complete_authorization(
        "code", first_state, request_id="first-start", now=NOW
    )
    assert first.session_token is not None
    identity.nonce = second_nonce
    with pytest.raises(AdministratorAuthError) as second:
        repository.complete_authorization(
            "code", second_state, request_id="second-start", now=NOW
        )
    assert second.value.code in {"invalid_token", "insufficient_scope"}
    with psycopg.connect(database_url) as connection:
        failed_redemptions = connection.execute(
            """
            SELECT count(*) FROM router.audit_events
            WHERE action = 'administrator.trusted_grant.failure'
              AND permission_result = 'denied'
            """
        ).fetchone()
    assert failed_redemptions == (1,)


def test_recovery_is_blocked_while_any_eligible_global_administrator_remains(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Do not use a narrow operation set to bypass recovery eligibility."""
    repository, identity = auth_repository
    _bootstrap(repository, identity, frozenset({"grant.manage"}))
    with pytest.raises(AdministratorAuthError) as blocked:
        repository.create_trusted_grant_url(
            TrustedGrantPurpose.RECOVERY,
            frozenset({"grant.manage", "credential.manage"}),
            request_id="blocked-recovery",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
    assert blocked.value.code == "insufficient_scope"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE router.administrator_grants SET revoked_at = %s",
            (NOW,),
        )
        connection.commit()
    recovery = repository.create_trusted_grant_url(
        TrustedGrantPurpose.RECOVERY,
        frozenset({"grant.manage"}),
        request_id="permitted-recovery",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert recovery.expires_at == NOW + timedelta(minutes=5)


def test_trusted_callback_failure_and_success_have_distinct_audits(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Audit both a trusted callback failure and a complete trusted success."""
    repository, identity = auth_repository
    trusted = repository.create_trusted_grant_url(
        TrustedGrantPurpose.INITIAL,
        frozenset({"grant.manage"}),
        request_id="trusted-audit-create",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    token = parse_qs(urlsplit(trusted.url).fragment)["token"][0]
    failed_state, _ = _start(repository, identity, trusted_token=token)
    identity.token_type = "access-token"
    with pytest.raises(AdministratorAuthError):
        repository.complete_authorization(
            "bad-code", failed_state, request_id="trusted-audit-fail", now=NOW
        )
    identity.token_type = "id-token"
    success_state, _ = _start(repository, identity, trusted_token=token)
    repository.complete_authorization(
        "good-code", success_state, request_id="trusted-audit-success", now=NOW
    )
    with psycopg.connect(database_url) as connection:
        actions = connection.execute(
            """
            SELECT action, permission_result FROM router.audit_events
            WHERE action IN (
                'administrator.trusted_grant.failure',
                'administrator.trusted_grant.redeem',
                'administrator.trusted_grant.success'
            )
            ORDER BY action
            """
        ).fetchall()
    assert actions == [
        ("administrator.trusted_grant.failure", "denied"),
        ("administrator.trusted_grant.redeem", "permitted"),
        ("administrator.trusted_grant.success", "permitted"),
    ]


def test_csrf_origin_and_grant_escalation_fail_before_change(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    session, csrf = _bootstrap(
        repository, identity, frozenset({"grant.manage", "health.read"})
    )
    request = GrantRequest(
        issuer=ISSUER,
        subject="person-2",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        operations=frozenset({"credential.manage"}),
        reason="Test escalation denial",
    )
    for token, origin in (("wrong", ORIGIN), (csrf, "https://other.example.test")):
        with pytest.raises(AdministratorAuthError):
            repository.create_grant(
                session,
                token,
                origin,
                request,
                "browser-proof-key",
                request_id="browser-proof",
                now=NOW,
            )
    with pytest.raises(AdministratorAuthError) as escalation:
        repository.create_grant(
            session,
            csrf,
            ORIGIN,
            request,
            "escalation-key-1",
            request_id="escalation",
            now=NOW,
        )
    assert escalation.value.code == "insufficient_scope"
    invalid_expiry = GrantRequest(
        issuer=ISSUER,
        subject="person-2",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        operations=frozenset({"health.read"}),
        reason="Test invalid expiry denial",
        expires_at=NOW,
    )
    with pytest.raises(AdministratorAuthError) as invalid:
        repository.create_grant(
            session,
            csrf,
            ORIGIN,
            invalid_expiry,
            "invalid-expiry-key",
            request_id="invalid-expiry",
            now=NOW,
        )
    assert invalid.value.code == "invalid_request"
    assert len(repository.list_grants(session, request_id="list", now=NOW)) == 1
    with psycopg.connect(database_url) as connection:
        denied = connection.execute(
            """
            SELECT count(*) FROM router.audit_events
            WHERE permission_result = 'denied'
              AND action IN (
                'administrator.grant.create', 'administrator.grant.manage'
              )
            """
        ).fetchone()
    assert denied is not None and denied[0] >= 3
    with psycopg.connect(database_url) as connection:
        invalid_audit = connection.execute(
            """
            SELECT count(*) FROM router.audit_events
            WHERE action = 'administrator.grant.create'
              AND permission_result = 'denied'
              AND safe_details ->> 'safe_error_code' = 'invalid_request'
            """
        ).fetchone()
    assert invalid_audit == (1,)


def test_grant_list_requires_grant_management_authority(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Do not convert missing list authority to an empty successful page."""
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    with pytest.raises(AdministratorAuthError) as denied:
        repository.list_grants(session, request_id="list-denied", now=NOW)
    assert denied.value.code == "insufficient_scope"


def test_grant_creation_is_durably_idempotent_and_contract_bound(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Replay equal content and reject changed content and create revisions."""
    repository, identity = auth_repository
    session, csrf = _bootstrap(
        repository, identity, frozenset({"grant.manage", "health.read"})
    )
    request = GrantRequest(
        issuer=ISSUER,
        subject="idempotent-person",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        operations=frozenset({"health.read"}),
        reason="Create one idempotent grant",
    )
    first = repository.create_grant(
        session,
        csrf,
        ORIGIN,
        request,
        "same-grant-key-1",
        request_id="grant-first",
        now=NOW,
    )
    replay = repository.create_grant(
        session,
        csrf,
        ORIGIN,
        request,
        "same-grant-key-1",
        request_id="grant-replay",
        now=NOW,
    )
    assert replay == first
    changed = GrantRequest(
        issuer=ISSUER,
        subject="changed-person",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        operations=frozenset({"health.read"}),
        reason="Change one idempotent grant",
    )
    with pytest.raises(AdministratorAuthError) as conflict:
        repository.create_grant(
            session,
            csrf,
            ORIGIN,
            changed,
            "same-grant-key-1",
            request_id="grant-conflict",
            now=NOW,
        )
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(AdministratorAuthError) as revision:
        repository.create_grant(
            session,
            csrf,
            ORIGIN,
            GrantRequest(
                issuer=ISSUER,
                subject="revision-person",
                authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
                operations=frozenset({"health.read"}),
                reason="Reject a create revision",
                expected_revision="1",
            ),
            "create-revision-key",
            request_id="grant-revision",
            now=NOW,
        )
    assert revision.value.code == "invalid_request"
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM router.administrator_grant_idempotency_bindings"
        ).fetchone()
    assert count == (1,)


def test_workspace_limited_grant_cannot_expand_to_service_scope(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Keep authorization, delegation, and list visibility in one workspace."""
    repository, identity = auth_repository
    administrator_id = "0198a080-0000-7000-8000-000000000090"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO router.administrators (id, issuer, subject)
            VALUES (%s, %s, 'workspace-admin')
            """,
            (administrator_id, ISSUER),
        )
        other_administrator_id = "0198a080-0000-7000-8000-000000000094"
        connection.execute(
            """
            INSERT INTO router.administrators (id, issuer, subject)
            VALUES (%s, %s, 'other-admin')
            """,
            (other_administrator_id, ISSUER),
        )
        for grant_id, owner_id, workspaces, operations in (
            (
                "0198a080-0000-7000-8000-000000000091",
                administrator_id,
                [WORKSPACE_ID],
                ["grant.manage", "health.read"],
            ),
            (
                "0198a080-0000-7000-8000-000000000092",
                administrator_id,
                [WORKSPACE_ID],
                ["health.read"],
            ),
            (
                "0198a080-0000-7000-8000-000000000093",
                other_administrator_id,
                [],
                ["health.read"],
            ),
        ):
            connection.execute(
                """
                INSERT INTO router.administrator_grants (
                    id, administrator_id, authority_class, service_id,
                    workspace_id, workspace_ids, operations, created_at
                ) VALUES (%s, %s, 'service', %s, %s, %s, %s, %s)
                """,
                (
                    grant_id,
                    owner_id,
                    SERVICE_ID,
                    workspaces[0] if workspaces else None,
                    workspaces,
                    operations,
                    NOW,
                ),
            )
        connection.commit()
    identity.subject = "workspace-admin"
    state, _ = _start(repository, identity)
    result = repository.complete_authorization(
        "code", state, request_id="workspace-login", now=NOW
    )
    assert result.session_token is not None and result.csrf_token is not None
    token, csrf = result.session_token.value, result.csrf_token.value
    workspace_principal = repository.authenticate_session(
        token,
        request_id="workspace-authority",
        now=NOW,
        policy=_policy("health.read"),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert workspace_principal.allowed_workspace_ids == frozenset({WORKSPACE_ID})
    with pytest.raises(AdministratorAuthError):
        repository.authenticate_session(
            token,
            request_id="service-escalation",
            now=NOW,
            policy=_policy("health.read"),
            service_id=SERVICE_ID,
        )
    visible_ids = {
        grant.grant_id
        for grant in repository.list_grants(token, request_id="visible", now=NOW)
    }
    assert "0198a080-0000-7000-8000-000000000092" in visible_ids
    assert "0198a080-0000-7000-8000-000000000093" not in visible_ids
    whole_service = GrantRequest(
        issuer=ISSUER,
        subject="person-3",
        authority_class=AuthorityClass.SERVICE,
        operations=frozenset({"health.read"}),
        reason="Attempt a service-wide delegation",
        service_id=SERVICE_ID,
    )
    with pytest.raises(AdministratorAuthError) as denied:
        repository.create_grant(
            token,
            csrf,
            ORIGIN,
            whole_service,
            "workspace-delegation-key",
            request_id="workspace-delegation",
            now=NOW,
        )
    assert denied.value.code == "insufficient_scope"


def test_whole_service_grant_can_use_and_delegate_a_workspace(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Keep an empty workspace set as complete service authority."""
    repository, identity = auth_repository
    with psycopg.connect(database_url) as connection:
        administrator_id = "0198a080-0000-7000-8000-000000000095"
        connection.execute(
            """
            INSERT INTO router.administrators (id, issuer, subject)
            VALUES (%s, %s, 'service-admin')
            """,
            (administrator_id, ISSUER),
        )
        connection.execute(
            """
            INSERT INTO router.administrator_grants (
                id, administrator_id, authority_class, service_id,
                workspace_ids, operations, created_at
            ) VALUES (%s, %s, 'service', %s, '{}', %s, %s)
            """,
            (
                "0198a080-0000-7000-8000-000000000096",
                administrator_id,
                SERVICE_ID,
                ["grant.manage", "health.read"],
                NOW,
            ),
        )
        connection.commit()
    identity.subject = "service-admin"
    state, _ = _start(repository, identity)
    result = repository.complete_authorization(
        "code", state, request_id="service-admin-login", now=NOW
    )
    assert result.session_token is not None and result.csrf_token is not None
    token, csrf = result.session_token.value, result.csrf_token.value
    principal = repository.authenticate_session(
        token,
        request_id="whole-service-workspace",
        now=NOW,
        policy=_policy("health.read"),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert principal.allowed_workspace_ids is None
    child = repository.create_grant(
        token,
        csrf,
        ORIGIN,
        GrantRequest(
            issuer=ISSUER,
            subject="workspace-child",
            authority_class=AuthorityClass.SERVICE,
            operations=frozenset({"health.read"}),
            reason="Delegate one workspace",
            service_id=SERVICE_ID,
            workspace_ids=frozenset({WORKSPACE_ID}),
        ),
        "delegate-workspace-key",
        request_id="delegate-workspace",
        now=NOW,
    )
    assert child.workspace_ids == frozenset({WORKSPACE_ID})


def test_disablement_persists_session_invalidation_and_outage_fails_closed(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    identity.active = False
    with pytest.raises(AdministratorAuthError) as disabled:
        repository.authenticate_session(
            session,
            request_id="disabled",
            now=NOW + timedelta(minutes=1),
            policy=_policy("health.read", sensitive=True),
        )
    assert disabled.value.code == "invalid_token"
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT revoked_at FROM router.administrator_sessions"
        ).fetchone()
    assert row is not None and row[0] == NOW + timedelta(minutes=1)
    identity.is_available = False
    with pytest.raises(AdministratorAuthError) as outage:
        repository.start_authorization(
            AuthenticationPurpose.LOGIN,
            "/admin",
            request_id="outage",
            now=NOW,
        )
    assert outage.value.code == "temporarily_unavailable"


def test_identity_outage_uses_only_a_fresh_cache_for_non_sensitive_work(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Permit cached reads for five minutes and reject sensitive or stale work."""
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    identity.is_available = False
    with pytest.raises(AdministratorAuthError) as sensitive:
        repository.authenticate_session(
            session,
            request_id="sensitive-outage",
            now=NOW + timedelta(minutes=1),
            policy=_policy("health.read", sensitive=True),
        )
    assert sensitive.value.code == "temporarily_unavailable"
    with psycopg.connect(database_url) as connection:
        audit = connection.execute(
            """
            SELECT permission_result FROM router.audit_events
            WHERE action = 'health.read'
            ORDER BY ingested_at DESC LIMIT 1
            """
        ).fetchone()
    assert audit == ("denied",)
    principal = repository.authenticate_session(
        session,
        request_id="cached-read",
        now=NOW + timedelta(minutes=5),
        policy=_policy("health.read"),
    )
    assert principal.subject == identity.subject
    with pytest.raises(AdministratorAuthError) as stale:
        repository.authenticate_session(
            session,
            request_id="stale-outage",
            now=NOW + timedelta(minutes=5, microseconds=1),
            policy=_policy("health.read"),
        )
    assert stale.value.code == "temporarily_unavailable"


@pytest.mark.parametrize("failure", ["invalid", "after_rotation"])
def test_provider_session_failure_revokes_and_clears_tokens(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
    failure: str,
) -> None:
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    identity.reject_provider_session = failure == "invalid"
    identity.rotate_provider_session = failure == "after_rotation"
    identity.fail_after_rotation = failure == "after_rotation"
    with pytest.raises(AdministratorAuthError) as rejected:
        repository.authenticate_session(
            session,
            request_id=f"provider-{failure}",
            now=NOW + timedelta(minutes=6),
            policy=_policy("health.read"),
        )
    assert rejected.value.code == "invalid_token"
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT revoked_at IS NOT NULL,
                   provider_access_token_ciphertext IS NULL,
                   provider_refresh_token_ciphertext IS NULL,
                   provider_access_expires_at IS NULL
            FROM router.administrator_sessions
            """
        ).fetchone()
    assert row == (True, True, True, True)


def test_expired_session_is_revoked_and_provider_tokens_are_cleared(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    with pytest.raises(AdministratorAuthError) as expired:
        repository.authenticate_session(
            session,
            request_id="expired",
            now=NOW + timedelta(minutes=15),
            policy=_policy("health.read"),
        )
    assert expired.value.code == "invalid_token"
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT revoked_at IS NOT NULL,
                   provider_access_token_ciphertext IS NULL,
                   provider_refresh_token_ciphertext IS NULL,
                   provider_access_expires_at IS NULL
            FROM router.administrator_sessions
            """
        ).fetchone()
    assert row == (True, True, True, True)


def test_session_expiry_logout_cookie_and_migration_workspace_controls(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    repository, identity = auth_repository
    session, csrf = _bootstrap(repository, identity, frozenset({"health.read"}))
    cookie = administrator_session_cookie(session)
    assert cookie.startswith("__Host-llmrouter-admin=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Domain" not in cookie
    with pytest.raises(AdministratorAuthError):
        repository.logout(
            session,
            csrf,
            "https://wrong.example.test",
            request_id="bad-logout",
            now=NOW,
        )
    repository.logout(session, csrf, ORIGIN, request_id="logout", now=NOW)
    with psycopg.connect(database_url) as connection:
        logout_failure = connection.execute(
            """
            SELECT count(*) FROM router.audit_events
            WHERE action = 'administrator.session.logout'
              AND permission_result = 'denied'
            """
        ).fetchone()
        assert logout_failure == (1,)
        administrator_row = connection.execute(
            "SELECT id FROM router.administrators"
        ).fetchone()
        assert administrator_row is not None
        administrator_id = administrator_row[0]
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO router.administrator_grants (
                    id, administrator_id, authority_class, service_id, workspace_ids,
                    operations
                ) VALUES (gen_random_uuid(), %s, 'service', %s, %s, %s)
                """,
                (
                    administrator_id,
                    SERVICE_ID,
                    [WORKSPACE_ID, WORKSPACE_ID],
                    ["health.read"],
                ),
            )
        connection.rollback()
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO router.administrator_grants (
                    id, administrator_id, authority_class, service_id, workspace_ids,
                    operations
                ) VALUES (gen_random_uuid(), %s, 'service', %s, %s, %s)
                """,
                (administrator_id, SERVICE_ID, [OTHER_WORKSPACE_ID], ["health.read"]),
            )
        connection.rollback()
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO router.administrator_grants (
                    id, administrator_id, authority_class, service_id, workspace_ids,
                    operations
                ) VALUES (
                    gen_random_uuid(), %s, 'service', %s,
                    ARRAY(
                        SELECT lpad(to_hex(item), 32, '0')::uuid
                        FROM generate_series(1, 1001) AS sequence(item)
                    ),
                    %s
                )
                """,
                (administrator_id, SERVICE_ID, ["health.read"]),
            )


def test_absolute_session_expiry_is_terminal(
    database_url: str,
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Reject a session at its exact eight-hour absolute expiry."""
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"health.read"}))
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE router.administrator_sessions
            SET last_used_at = %s, idle_expires_at = absolute_expires_at
            """,
            (NOW + timedelta(hours=7, minutes=59),),
        )
        connection.commit()
    with pytest.raises(AdministratorAuthError) as expired:
        repository.authenticate_session(
            session,
            request_id="absolute-expiry",
            now=NOW + timedelta(hours=8),
            policy=_policy("health.read"),
        )
    assert expired.value.code == "invalid_token"


def test_grant_list_activity_extends_idle_expiry(
    auth_repository: tuple[AdministratorAuthRepository, FakeIdentityService],
) -> None:
    """Keep an active grant reader signed in until 15 minutes after its activity."""
    repository, identity = auth_repository
    session, _ = _bootstrap(repository, identity, frozenset({"grant.manage"}))
    repository.list_grants(
        session, request_id="active-list", now=NOW + timedelta(minutes=14)
    )
    current = repository.get_session(
        session, request_id="after-active-list", now=NOW + timedelta(minutes=16)
    )
    assert current.subject == identity.subject

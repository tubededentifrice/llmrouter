"""PostgreSQL tests for service machine credentials and token authority."""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from argon2 import PasswordHasher
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.machine_identity import (
    DEFAULT_ROTATION_OVERLAP,
    BootstrapScope,
    DigestKeyCustodyState,
    MachineCredentialRepository,
    MachineIdentityError,
    TLSClientIdentity,
    TokenExchange,
    WorkspaceLimit,
)

from .helpers import (
    OTHER_SERVICE_ID,
    OTHER_WORKSPACE_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    seed_scope,
)

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
DIGEST_KEY = bytes(range(32))
SECRET_LENGTH = 43
SECOND_GENERATION = 2
GENERATED_SECRET_COUNT = 3


def _admin_context() -> RequestContext:
    return RequestContext(
        request_id="credential-admin-request",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="issuer:administrator",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="credential.manage",
        scope=Scope(),
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=True,
    )


def _scope(*, explicit: bool = False) -> BootstrapScope:
    return BootstrapScope(
        audiences=frozenset({Audience.SERVICE_MANAGEMENT}),
        operations=frozenset({"workspace.create", "workspace.read"}),
        workspace_limit=(
            WorkspaceLimit.EXPLICIT_ONLY
            if explicit
            else WorkspaceLimit.ALL_SERVICE_WORKSPACES
        ),
    )


@pytest.fixture
def repository(database_url: str) -> MachineCredentialRepository:
    """Apply the schema and return one repository with deployment-held keys."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    return MachineCredentialRepository(
        database_url,
        issuer="https://router.example.test",
        digest_keys={"key-2026-08": DIGEST_KEY},
        current_digest_key_id="key-2026-08",
    )


def _exchange(  # noqa: PLR0913
    repository: MachineCredentialRepository,
    service_id: str,
    secret: str,
    *,
    now: datetime = NOW,
    workspace_ids: frozenset[str] | None = None,
    tls_identity: TLSClientIdentity | None = None,
    operations: frozenset[str] = frozenset({"workspace.read"}),
) -> TokenExchange:
    return repository.exchange(
        request_id="token-exchange-request",
        service_id=service_id,
        bootstrap_secret=secret,
        audience=Audience.SERVICE_MANAGEMENT,
        operations=operations,
        workspace_ids=workspace_ids,
        tls_identity=tls_identity,
        now=now,
    )


def _tls_identity(
    service_id: str,
    generation: int,
    *,
    name: str = "spiffe://router/service-a/generation-1",
) -> TLSClientIdentity:
    return TLSClientIdentity(
        certificate_identity=name,
        service_id=service_id,
        credential_generation=generation,
        tls_version="TLSv1.3",
        private_trust_anchor=True,
        server_certificate_validated=True,
        client_certificate_validated=True,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def test_bootstrap_and_token_storage_never_store_or_represent_secrets(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Store a salted verifier and keyed digest, with redacted values in text."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    secret = created.secret.value
    assert len(secret) == SECRET_LENGTH
    assert secret not in repr(created)

    exchanged = _exchange(repository, SERVICE_ID, secret)
    token = exchanged.access_token.value
    assert len(token) == SECRET_LENGTH
    assert token not in repr(exchanged)

    with psycopg.connect(database_url) as connection:
        verifier = connection.execute(
            "SELECT argon2id_verifier FROM router.service_bootstrap_generations"
        ).fetchone()
        stored = connection.execute(
            """
            SELECT token_digest, digest_key_id, issuer, issued_at, expires_at
            FROM router.service_access_tokens
            """
        ).fetchone()
    assert verifier is not None
    assert verifier[0].startswith("$argon2id$")
    assert secret not in verifier[0]
    assert PasswordHasher().verify(verifier[0], secret)
    assert stored is not None
    assert bytes(stored[0]) != token.encode()
    assert stored[1:3] == ("key-2026-08", "https://router.example.test")
    assert stored[4] - stored[3] == timedelta(minutes=5)


def test_secrets_have_256_bit_form_and_do_not_repeat(
    repository: MachineCredentialRepository,
) -> None:
    """Generate independent 32-byte base64url bootstrap and token secrets."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    first = _exchange(repository, SERVICE_ID, created.secret.value)
    second = _exchange(repository, SERVICE_ID, created.secret.value)
    values = {created.secret.value, first.access_token.value, second.access_token.value}
    assert len(values) == GENERATED_SECRET_COUNT
    assert all(len(value) == SECRET_LENGTH for value in values)
    with pytest.raises(MachineIdentityError) as bootstrap_as_token:
        repository.authenticate(
            created.secret.value, request_id="bootstrap-as-token", now=NOW
        )
    assert bootstrap_as_token.value.code == "invalid_token"
    with pytest.raises(MachineIdentityError) as token_as_bootstrap:
        _exchange(repository, SERVICE_ID, first.access_token.value)
    assert token_as_bootstrap.value.code == "invalid_token"


def test_authentication_returns_all_exact_bound_claims_and_clock_skew(
    repository: MachineCredentialRepository,
) -> None:
    """Bind the opaque token to all private claims and at most 30 seconds skew."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(explicit=True), now=NOW
    )
    with pytest.raises(MachineIdentityError) as omitted:
        _exchange(repository, SERVICE_ID, created.secret.value)
    assert omitted.value.code == "insufficient_scope"
    exchanged = _exchange(
        repository,
        SERVICE_ID,
        created.secret.value,
        workspace_ids=frozenset({WORKSPACE_ID}),
    )
    principal = repository.authenticate(
        exchanged.access_token.value,
        request_id="authenticate-request",
        now=NOW + timedelta(minutes=5, seconds=30),
    )
    assert principal.issuer == "https://router.example.test"
    assert principal.token_id
    assert principal.audience is Audience.SERVICE_MANAGEMENT
    assert principal.service_id == SERVICE_ID
    assert principal.operations == frozenset({"workspace.read"})
    assert principal.allowed_workspace_ids == frozenset({WORKSPACE_ID})
    assert principal.credential_generation == 1
    with pytest.raises(MachineIdentityError) as error:
        repository.authenticate(
            exchanged.access_token.value,
            request_id="authenticate-late",
            now=NOW + timedelta(minutes=5, seconds=30, microseconds=1),
        )
    assert error.value.code == "invalid_token"
    assert exchanged.access_token.value not in str(error.value)


def test_explicit_workspace_scope_cannot_issue_workspace_create(
    repository: MachineCredentialRepository,
) -> None:
    """Do not let an explicit workspace grant create a new service-wide scope."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(explicit=True), now=NOW
    )
    with pytest.raises(MachineIdentityError) as omitted:
        _exchange(repository, SERVICE_ID, created.secret.value)
    assert omitted.value.code == "insufficient_scope"
    with pytest.raises(MachineIdentityError) as denied:
        _exchange(
            repository,
            SERVICE_ID,
            created.secret.value,
            workspace_ids=frozenset({WORKSPACE_ID}),
            operations=frozenset({"workspace.create"}),
        )
    assert denied.value.code == "insufficient_scope"


def test_empty_workspace_restriction_does_not_expand_to_all_workspaces(
    repository: MachineCredentialRepository,
) -> None:
    """Keep an explicit empty workspace set distinct from an omitted limit."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    exchanged = _exchange(
        repository,
        SERVICE_ID,
        created.secret.value,
        workspace_ids=frozenset(),
    )
    principal = repository.authenticate(
        exchanged.access_token.value, request_id="empty-scope", now=NOW
    )
    assert principal.allowed_workspace_ids == frozenset()


def test_scope_matrix_and_service_workspace_isolation_fail_closed(
    repository: MachineCredentialRepository,
) -> None:
    """Reject cross-audience operations and workspaces from another service."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    with pytest.raises(MachineIdentityError) as wrong_operation:
        _exchange(
            repository,
            SERVICE_ID,
            created.secret.value,
            operations=frozenset({"model.read"}),
        )
    assert wrong_operation.value.code == "insufficient_scope"

    with pytest.raises(MachineIdentityError) as wrong_workspace:
        _exchange(
            repository,
            SERVICE_ID,
            created.secret.value,
            workspace_ids=frozenset({OTHER_WORKSPACE_ID}),
        )
    assert wrong_workspace.value.code == "insufficient_scope"


def test_rotation_overlap_early_end_and_revocation_invalidate_tokens(
    repository: MachineCredentialRepository,
) -> None:
    """Support the default overlap, early end, and no-overlap revocation."""
    first = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    first_token = _exchange(repository, SERVICE_ID, first.secret.value)
    second = repository.rotate_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW + timedelta(minutes=1)
    )
    assert second.generation == SECOND_GENERATION
    assert second.prior_generation_expires_at == NOW + timedelta(minutes=1) + (
        DEFAULT_ROTATION_OVERLAP
    )
    _exchange(
        repository,
        SERVICE_ID,
        first.secret.value,
        now=NOW + timedelta(minutes=2),
    )
    repository.end_overlap(
        _admin_context(), SERVICE_ID, 1, now=NOW + timedelta(minutes=2)
    )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(
            first_token.access_token.value,
            request_id="old-token",
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(MachineIdentityError):
        _exchange(
            repository,
            SERVICE_ID,
            first.secret.value,
            now=NOW + timedelta(minutes=2),
        )

    second_token = _exchange(
        repository,
        SERVICE_ID,
        second.secret.value,
        now=NOW + timedelta(minutes=2),
    )
    repository.revoke_generation(
        _admin_context(), SERVICE_ID, 2, now=NOW + timedelta(minutes=3)
    )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(
            second_token.access_token.value,
            request_id="revoked-token",
            now=NOW + timedelta(minutes=3),
        )
    with pytest.raises(MachineIdentityError):
        _exchange(
            repository,
            SERVICE_ID,
            second.secret.value,
            now=NOW + timedelta(minutes=3),
        )


def test_rotation_bounds_and_recovery_after_full_revocation(
    repository: MachineCredentialRepository,
) -> None:
    """Reject invalid overlap values and replace a fully revoked credential."""
    repository.create_initial_bootstrap(_admin_context(), SERVICE_ID, _scope(), now=NOW)
    for overlap in (timedelta(microseconds=-1), timedelta(hours=24, microseconds=1)):
        with pytest.raises(ValueError, match="zero to 24 hours"):
            repository.rotate_bootstrap(
                _admin_context(), SERVICE_ID, _scope(), now=NOW, overlap=overlap
            )
    repository.revoke_generation(_admin_context(), SERVICE_ID, 1, now=NOW)
    replacement = repository.rotate_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW + timedelta(seconds=1)
    )
    assert replacement.generation == SECOND_GENERATION
    assert replacement.prior_generation_expires_at is None


def test_rapid_rotation_keeps_only_current_and_prior_generations(
    repository: MachineCredentialRepository,
) -> None:
    """End generations older than the one immediately before the current one."""
    first = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    first_token = _exchange(repository, SERVICE_ID, first.secret.value)
    second = repository.rotate_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW + timedelta(minutes=1)
    )
    second_token = _exchange(
        repository, SERVICE_ID, second.secret.value, now=NOW + timedelta(minutes=1)
    )
    third = repository.rotate_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW + timedelta(minutes=2)
    )
    with pytest.raises(MachineIdentityError):
        _exchange(
            repository,
            SERVICE_ID,
            first.secret.value,
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(
            first_token.access_token.value,
            request_id="generation-one-token",
            now=NOW + timedelta(minutes=2),
        )
    repository.authenticate(
        second_token.access_token.value,
        request_id="generation-two-token",
        now=NOW + timedelta(minutes=2),
    )
    _exchange(
        repository, SERVICE_ID, third.secret.value, now=NOW + timedelta(minutes=2)
    )


def test_exchange_fails_when_generation_cannot_cover_full_token_lifetime(
    repository: MachineCredentialRepository,
) -> None:
    """Never issue a token that can outlive its bootstrap generation."""
    first = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    repository.rotate_bootstrap(
        _admin_context(),
        SERVICE_ID,
        _scope(),
        now=NOW + timedelta(minutes=1),
        overlap=timedelta(minutes=4),
    )
    with pytest.raises(MachineIdentityError) as error:
        _exchange(
            repository,
            SERVICE_ID,
            first.secret.value,
            now=NOW + timedelta(minutes=1, seconds=1),
        )
    assert error.value.code == "invalid_token"


def test_credential_change_rejects_stale_or_future_recent_authentication(
    repository: MachineCredentialRepository,
) -> None:
    """Require recent administrator authentication inside a five-minute window."""
    context = _admin_context()
    invalid_recent_times = (
        NOW - timedelta(minutes=5, microseconds=1),
        NOW + timedelta(seconds=1),
    )
    for recent in invalid_recent_times:
        with pytest.raises(MachineIdentityError) as error:
            repository.create_initial_bootstrap(
                replace(context, recent_authentication_at=recent),
                SERVICE_ID,
                _scope(),
                now=NOW,
            )
        assert error.value.code == "insufficient_scope"


def test_tls_identity_must_match_service_and_generation_before_scope(
    repository: MachineCredentialRepository,
) -> None:
    """Reject a wrong TLS binding before an invalid operation is considered."""
    first = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    other = repository.create_initial_bootstrap(
        _admin_context(), OTHER_SERVICE_ID, _scope(), now=NOW
    )
    other_tls = _tls_identity(
        OTHER_SERVICE_ID,
        1,
        name="spiffe://router/service-b/generation-1",
    )
    repository.register_tls_identity(_admin_context(), other_tls, now=NOW)
    repository.set_tls_policy(_admin_context(), SERVICE_ID, required=True, now=NOW)

    with pytest.raises(MachineIdentityError) as wrong_service:
        _exchange(
            repository,
            SERVICE_ID,
            first.secret.value,
            tls_identity=other_tls,
            operations=frozenset({"model.read"}),
        )
    assert wrong_service.value.code == "invalid_token"
    assert other.secret.value not in str(wrong_service.value)

    valid_tls = _tls_identity(SERVICE_ID, 1)
    repository.register_tls_identity(_admin_context(), valid_tls, now=NOW)
    token = _exchange(
        repository, SERVICE_ID, first.secret.value, tls_identity=valid_tls
    )
    repository.revoke_tls_identity(
        _admin_context(), valid_tls.certificate_identity, now=NOW
    )
    with pytest.raises(MachineIdentityError) as revoked:
        repository.authenticate(
            token.access_token.value,
            request_id="revoked-tls",
            now=NOW,
            tls_identity=valid_tls,
        )
    assert revoked.value.code == "invalid_token"


def test_tls_registration_fails_safely_for_missing_or_invalid_generation(
    repository: MachineCredentialRepository,
) -> None:
    """Do not expose PostgreSQL relation details for invalid TLS registration."""
    missing = _tls_identity(SERVICE_ID, 7)
    with pytest.raises(MachineIdentityError) as error:
        repository.register_tls_identity(_admin_context(), missing, now=NOW)
    assert error.value.code == "not_found"
    assert "foreign key" not in str(error.value).lower()

    for identity in (
        replace(missing, credential_generation=1, issued_at=NOW + timedelta(seconds=1)),
        replace(missing, credential_generation=1, expires_at=NOW),
    ):
        with pytest.raises(MachineIdentityError) as invalid_time:
            repository.register_tls_identity(_admin_context(), identity, now=NOW)
        assert invalid_time.value.code == "not_found"


def test_tls_model_rejects_wrong_version_trust_and_time_profile() -> None:
    """Accept only aware, validated TLS 1.3 identities of at most 24 hours."""
    valid = _tls_identity(SERVICE_ID, 1)
    invalid_values = (
        {"tls_version": "TLSv1.2"},
        {"private_trust_anchor": False},
        {"expires_at": valid.issued_at + timedelta(hours=24, microseconds=1)},
        {
            "issued_at": valid.issued_at.replace(tzinfo=None),
            "expires_at": valid.expires_at.replace(tzinfo=None),
        },
    )
    for changes in invalid_values:
        with pytest.raises(ValueError, match=r"TLS|trust|time zone|24 hours"):
            replace(valid, **changes)


def test_invalid_time_token_format_disabled_service_and_missing_key_fail_closed(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Reject unsafe time, token, service, and key-custody states."""
    with pytest.raises(ValueError, match="time zone"):
        repository.create_initial_bootstrap(
            _admin_context(), SERVICE_ID, _scope(), now=NOW.replace(tzinfo=None)
        )
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    token = _exchange(repository, SERVICE_ID, created.secret.value)
    with pytest.raises(MachineIdentityError):
        repository.authenticate("x" * 200, request_id="bad-token", now=NOW)
    without_old_key = MachineCredentialRepository(
        database_url,
        issuer="https://router.example.test",
        digest_keys={"new-key": b"n" * 32},
        current_digest_key_id="new-key",
    )
    with pytest.raises(MachineIdentityError):
        without_old_key.authenticate(
            token.access_token.value, request_id="missing-key", now=NOW
        )
    with pytest.raises(ValueError, match="not available"):
        MachineCredentialRepository(
            database_url,
            issuer="https://router.example.test",
            digest_keys={"new-key": b"n" * 32},
            current_digest_key_id="missing-key",
        )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE router.services
            SET state = 'disabled', state_revision = state_revision + 1
            WHERE id = %s
            """,
            (SERVICE_ID,),
        )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(
            token.access_token.value, request_id="disabled", now=NOW
        )


def test_token_digest_key_rotation_accepts_staged_old_key(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Read a token with an old digest key while a new key issues new tokens."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    exchanged = _exchange(repository, SERVICE_ID, created.secret.value)
    rotated = MachineCredentialRepository(
        database_url,
        issuer="https://router.example.test",
        digest_keys={"key-2026-08": DIGEST_KEY, "key-2026-09": b"n" * 32},
        current_digest_key_id="key-2026-09",
    )
    principal = rotated.authenticate(
        exchanged.access_token.value,
        request_id="staged-digest-key",
        now=NOW,
    )
    assert principal.service_id == SERVICE_ID


def test_missing_digest_key_has_safe_operator_visible_state(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Report a missing active digest key identity without key material."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    _exchange(repository, SERVICE_ID, created.secret.value)
    without_old_key = MachineCredentialRepository(
        database_url,
        issuer="https://router.example.test",
        digest_keys={"new-key": b"n" * 32},
        current_digest_key_id="new-key",
    )
    status = without_old_key.digest_key_custody_status(now=NOW)
    assert status.state is DigestKeyCustodyState.DEGRADED
    assert status.missing_key_ids == ("key-2026-08",)
    assert DIGEST_KEY.hex() not in repr(status)


def test_authentication_waits_for_revoke_and_reads_revoked_state(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Do not authorize from a token snapshot that precedes a concurrent revoke."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    token = _exchange(repository, SERVICE_ID, created.secret.value)
    lock_name = f"machine-credential:{SERVICE_ID}"
    with psycopg.connect(database_url) as blocker:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_name,)
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                repository.authenticate,
                token.access_token.value,
                request_id="concurrent-revoke",
                now=NOW,
            )
            with psycopg.connect(database_url, autocommit=True) as observer:
                for _attempt in range(200):
                    waiting = observer.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE locktype = 'advisory' AND NOT granted
                        )
                        """
                    ).fetchone()
                    if waiting is not None and waiting[0]:
                        break
                    time.sleep(0.005)
                else:
                    pytest.fail("Authentication did not wait for the credential lock.")
            blocker.execute(
                """
                UPDATE router.service_bootstrap_generations
                SET revoked_at = %s, valid_until = %s
                WHERE service_id = %s AND generation = 1
                """,
                (NOW, NOW, SERVICE_ID),
            )
            blocker.execute(
                """
                UPDATE router.service_access_tokens SET revoked_at = %s
                WHERE service_id = %s
                """,
                (NOW, SERVICE_ID),
            )
            blocker.commit()
            with pytest.raises(MachineIdentityError) as revoked:
                future.result(timeout=5)
    assert revoked.value.code == "invalid_token"


def test_corrupt_verifier_and_future_generation_fail_as_invalid_token(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Map corrupt stored verification data and not-yet-valid rows to one safe error."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    with psycopg.connect(database_url) as connection:
        original = connection.execute(
            """
            SELECT argon2id_verifier
            FROM router.service_bootstrap_generations WHERE service_id = %s
            """,
            (SERVICE_ID,),
        ).fetchone()
        assert original is not None
        connection.execute(
            "ALTER TABLE router.service_bootstrap_generations DISABLE TRIGGER "
            "service_bootstrap_generation_protected"
        )
        connection.execute(
            """
            UPDATE router.service_bootstrap_generations
            SET argon2id_verifier = 'not-an-argon2-hash'
            WHERE service_id = %s
            """,
            (SERVICE_ID,),
        )
    with pytest.raises(MachineIdentityError) as corrupt:
        _exchange(repository, SERVICE_ID, created.secret.value)
    assert corrupt.value.code == "invalid_token"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE router.service_bootstrap_generations
            SET argon2id_verifier = %s, created_at = %s
            WHERE service_id = %s
            """,
            (original[0], NOW + timedelta(seconds=1), SERVICE_ID),
        )
    with pytest.raises(MachineIdentityError) as future:
        _exchange(repository, SERVICE_ID, created.secret.value)
    assert future.value.code == "invalid_token"


def test_issuer_confusion_fails_for_generation_and_token_rows(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Require the configured issuer on both referenced stored records."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    token = _exchange(repository, SERVICE_ID, created.secret.value)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "ALTER TABLE router.service_bootstrap_generations DISABLE TRIGGER "
            "service_bootstrap_generation_protected"
        )
        connection.execute(
            """
            UPDATE router.service_bootstrap_generations SET issuer = 'wrong-issuer'
            WHERE service_id = %s
            """,
            (SERVICE_ID,),
        )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(token.access_token.value, request_id="issuer", now=NOW)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE router.service_bootstrap_generations
            SET issuer = 'https://router.example.test'
            WHERE service_id = %s
            """,
            (SERVICE_ID,),
        )
        connection.execute(
            "ALTER TABLE router.service_access_tokens DISABLE TRIGGER "
            "service_access_token_protected"
        )
        connection.execute(
            "UPDATE router.service_access_tokens SET issuer = 'wrong-issuer'"
        )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(token.access_token.value, request_id="issuer", now=NOW)


def test_database_rejects_claim_confusion_and_authority_mutation(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Keep issued machine authority closed and immutable under direct SQL."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    token = _exchange(repository, SERVICE_ID, created.secret.value)
    with psycopg.connect(database_url) as connection:
        token_id = connection.execute(
            "SELECT token_id FROM router.service_access_tokens"
        ).fetchone()
        assert token_id is not None
        invalid_statements = (
            (
                (
                    "UPDATE router.service_bootstrap_generations "
                    "SET allowed_operations = ARRAY['model.read'] WHERE service_id = %s"
                ),
                (SERVICE_ID,),
            ),
            (
                (
                    "UPDATE router.service_access_tokens "
                    "SET operations = ARRAY['workspace.create'] WHERE token_id = %s"
                ),
                (token_id[0],),
            ),
            (
                (
                    "UPDATE router.service_bootstrap_generations "
                    "SET valid_until = %s WHERE service_id = %s"
                ),
                (datetime.now(UTC) + timedelta(days=365), SERVICE_ID),
            ),
        )
        for statement, parameters in invalid_statements:
            with pytest.raises(psycopg.Error), connection.transaction():
                connection.execute(statement, parameters)

        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                """
                INSERT INTO router.service_access_tokens (
                    token_id, token_digest, service_id, bootstrap_generation,
                    audience, operations, issued_at, expires_at, issuer,
                    digest_key_id, workspace_restricted
                ) VALUES (
                    %s, decode(repeat('05', 32), 'hex'), %s, 1,
                    'service_management', ARRAY['model.read'], %s, %s,
                    'https://router.example.test', 'key-2026-08', false
                )
                """,
                (uuid.uuid4(), SERVICE_ID, NOW, NOW + timedelta(minutes=5)),
            )

        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                """
                INSERT INTO router.service_access_tokens (
                    token_id, token_digest, service_id, bootstrap_generation,
                    audience, operations, issued_at, expires_at, issuer,
                    digest_key_id, workspace_restricted
                ) VALUES (
                    %s, decode(repeat('06', 32), 'hex'), %s, 1,
                    'service_management', ARRAY['workspace.read'], %s, %s,
                    'https://router.example.test', 'key-2026-08', false
                )
                """,
                (uuid.uuid4(), SERVICE_ID, NOW, NOW + timedelta(minutes=4)),
            )

        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                """
                INSERT INTO router.service_bootstrap_generations (
                    id, service_id, generation, argon2id_verifier,
                    allowed_operations, created_at, issuer,
                    allowed_audiences, workspace_limit
                ) VALUES (
                    %s, %s, 2, 'verifier', ARRAY['model.read'], %s,
                    'https://router.example.test',
                    ARRAY['service_management'], 'all_service_workspaces'
                )
                """,
                (uuid.uuid4(), SERVICE_ID, NOW),
            )

        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                """
                INSERT INTO router.service_access_tokens (
                    token_id, token_digest, service_id, bootstrap_generation,
                    audience, operations, workspace_ids, issued_at, expires_at,
                    issuer, digest_key_id, workspace_restricted
                ) VALUES (
                    %s, decode(repeat('07', 32), 'hex'), %s, 1,
                    'service_management', ARRAY['workspace.read'], ARRAY[%s]::uuid[],
                    %s, %s, 'https://router.example.test', 'key-2026-08', true
                )
                """,
                (
                    uuid.uuid4(),
                    SERVICE_ID,
                    OTHER_WORKSPACE_ID,
                    NOW,
                    NOW + timedelta(minutes=5),
                ),
            )

    repository.revoke_generation(_admin_context(), SERVICE_ID, 1, now=NOW)
    with psycopg.connect(database_url) as connection:
        for table in (
            "service_bootstrap_generations",
            "service_access_tokens",
        ):
            with pytest.raises(psycopg.Error), connection.transaction():
                connection.execute(
                    f"UPDATE router.{table} SET revoked_at = NULL"  # noqa: S608
                )
    with pytest.raises(MachineIdentityError):
        repository.authenticate(token.access_token.value, request_id="revoked", now=NOW)
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM router.service_access_tokens")
        with (
            pytest.raises(psycopg.Error, match="authority is immutable"),
            connection.transaction(),
        ):
            connection.execute(
                """
                DELETE FROM router.service_bootstrap_generations
                WHERE service_id = %s
                """,
                (SERVICE_ID,),
            )


def test_tls_identity_conflict_and_delete_fail_closed(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Keep one certificate identity stable and prevent delete-based restoration."""
    repository.create_initial_bootstrap(_admin_context(), SERVICE_ID, _scope(), now=NOW)
    identity = _tls_identity(SERVICE_ID, 1)
    repository.register_tls_identity(_admin_context(), identity, now=NOW)
    with pytest.raises(MachineIdentityError) as duplicate:
        repository.register_tls_identity(_admin_context(), identity, now=NOW)
    assert duplicate.value.code == "not_found"
    repository.revoke_tls_identity(
        _admin_context(), identity.certificate_identity, now=NOW
    )
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE router.service_machine_tls_identities SET revoked_at = NULL
            WHERE certificate_identity = %s
            """,
            (identity.certificate_identity,),
        )

    repository.set_tls_policy(_admin_context(), SERVICE_ID, required=True, now=NOW)
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error),
        connection.transaction(),
    ):
        connection.execute(
            "DELETE FROM router.service_machine_tls_policies WHERE service_id = %s",
            (SERVICE_ID,),
        )
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error),
        connection.transaction(),
    ):
        connection.execute(
            """
            DELETE FROM router.service_machine_tls_identities
            WHERE certificate_identity = %s
            """,
            (identity.certificate_identity,),
        )


def test_backdated_credential_changes_fail_safely(
    repository: MachineCredentialRepository,
) -> None:
    """Reject a credential mutation time before its authorized administrator time."""
    created = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    backdated = NOW - timedelta(seconds=1)
    operations = (
        lambda: repository.rotate_bootstrap(
            _admin_context(), SERVICE_ID, _scope(), now=backdated
        ),
        lambda: repository.revoke_generation(
            _admin_context(), SERVICE_ID, created.generation, now=backdated
        ),
        lambda: repository.set_tls_policy(
            _admin_context(), SERVICE_ID, required=True, now=backdated
        ),
    )
    for operation in operations:
        with pytest.raises(MachineIdentityError) as denied:
            operation()
        assert denied.value.code == "insufficient_scope"


def test_every_credential_mutation_creates_secret_free_global_audit(
    database_url: str, repository: MachineCredentialRepository
) -> None:
    """Audit each credential mutation with its exact safe actor and service scope."""
    first = repository.create_initial_bootstrap(
        _admin_context(), SERVICE_ID, _scope(), now=NOW
    )
    second = repository.rotate_bootstrap(
        _admin_context(), SERVICE_ID, _scope(explicit=True), now=NOW
    )
    repository.revoke_generation(_admin_context(), SERVICE_ID, 1, now=NOW)
    repository.set_tls_policy(_admin_context(), SERVICE_ID, required=True, now=NOW)
    identity = _tls_identity(
        SERVICE_ID,
        second.generation,
        name="spiffe://router/service-a/audit-certificate",
    )
    repository.register_tls_identity(_admin_context(), identity, now=NOW)
    repository.revoke_tls_identity(
        _admin_context(), identity.certificate_identity, now=NOW
    )
    with psycopg.connect(database_url) as connection:
        events = connection.execute(
            """
            SELECT action, audit_class, actor_kind, actor_id, authority_class,
                   service_id::text, permission_result, safe_details
            FROM router.audit_events
            WHERE service_id = %s
            ORDER BY action
            """,
            (SERVICE_ID,),
        ).fetchall()
    assert [event[0] for event in events] == sorted(
        [
            "credential.create",
            "credential.revoke",
            "credential.rotate",
            "credential.tls_identity",
            "credential.tls_policy",
            "credential.tls_revoke",
        ]
    )
    assert all(
        event[1:7]
        == (
            "global_administration",
            "administrator",
            "issuer:administrator",
            "global_administrator",
            SERVICE_ID,
            "permitted",
        )
        and event[7] == {}
        for event in events
    )
    serialized_events = repr(events)
    assert first.secret.value not in serialized_events
    assert identity.certificate_identity not in serialized_events

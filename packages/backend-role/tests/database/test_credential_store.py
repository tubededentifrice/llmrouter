"""Encrypted provider credential custody and distribution tests."""
# ruff: noqa: D103, PLR2004, S105

from __future__ import annotations

import concurrent.futures
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.credential_store import (
    BoundedCredentialCache,
    CredentialAction,
    CredentialMetadata,
    CredentialOwner,
    CredentialResult,
    CredentialState,
    CredentialStoreError,
    CredentialStoreErrorCode,
    DataPlaneCredentialDistributor,
    EncryptedCredentialRepository,
    SecretInput,
    WrappingKeyCustodyState,
)
from llmrouter_backend.credential_store.crypto import (
    EnvelopeCipher,
    EnvelopeDecryptionError,
)
from llmrouter_backend.credential_store.repository import _insert_invalidation
from llmrouter_backend.database import migrate
from psycopg.rows import dict_row

from .helpers import OTHER_SERVICE_ID, SERVICE_ID, seed_scope

NOW = datetime(2026, 8, 13, 15, tzinfo=UTC)
WRAPPING_KEY = bytes(range(32))
NEXT_WRAPPING_KEY = bytes(range(32, 64))
DIGEST_KEY = bytes(reversed(range(32)))
PROVIDER_ID = "provider.example"
ADAPTER_ID = PROVIDER_ID
MODEL_ID = "0198a080-0000-7000-8000-000000000071"
INSTANCE_ID = "0198a080-0000-7000-8000-000000000072"
ROUTE_ID = "0198a080-0000-7000-8000-000000000073"


def _context(*, mutation: bool = True) -> RequestContext:
    return RequestContext(
        request_id="credential-store-request",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="issuer:administrator",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="credential.manage",
        scope=Scope(),
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=mutation,
    )


@pytest.fixture
def repository(database_url: str) -> EncryptedCredentialRepository:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    return EncryptedCredentialRepository(
        database_url,
        wrapping_keys={"wrap-1": WRAPPING_KEY},
        current_wrapping_key_id="wrap-1",
        idempotency_digest_key=DIGEST_KEY,
    )


def _create(
    repository: EncryptedCredentialRepository,
    *,
    key: str = "credential-create-key",
    value: str = "top-secret-provider-value",
    owner: CredentialOwner | None = None,
) -> CredentialResult:
    return repository.create(
        _context(),
        idempotency_key=key,
        owner=CredentialOwner() if owner is None else owner,
        provider_catalog_id=PROVIDER_ID,
        secret=SecretInput(value),
        safe_label="Test provider",
        now=NOW,
    )


def _seed_route(database_url: str, credential_id: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO router.provider_adapter_types (
                id, settings_schema_name, settings_schema_major, capabilities
            ) VALUES (%s, 'settings', 1, '{}')
            """,
            (ADAPTER_ID,),
        )
        connection.execute(
            """
            INSERT INTO router.canonical_models (id, stable_name, capabilities)
            VALUES (%s, 'model-a', '{}')
            """,
            (MODEL_ID,),
        )
        connection.execute(
            """
            INSERT INTO router.provider_instances (
                id, owner_kind, owner_service_id, adapter_type_id,
                credential_id, stable_name, endpoint_origin,
                settings_schema_name, settings_schema_major, settings
            ) VALUES (
                %s, 'global', NULL, %s, %s, 'provider-a',
                'https://provider.example', 'settings', 1, '{}'
            )
            """,
            (INSTANCE_ID, ADAPTER_ID, credential_id),
        )
        connection.execute(
            """
            INSERT INTO router.provider_model_routes (
                id, owner_kind, owner_service_id, provider_instance_id,
                canonical_model_id, provider_lookup_id,
                settings_schema_name, settings_schema_major, settings
            ) VALUES (
                %s, 'global', NULL, %s, %s, 'model-a', 'settings', 1, '{}'
            )
            """,
            (ROUTE_ID, INSTANCE_ID, MODEL_ID),
        )


def test_create_is_encrypted_write_only_and_idempotent(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    secret = "small-low-entropy-value"
    created = _create(repository, value=secret)
    replay = _create(repository, value=secret)
    assert replay.replayed
    assert replay.metadata == created.metadata
    assert not hasattr(created.metadata, "safe_label")
    assert secret not in repr(SecretInput(secret))
    assert secret not in repr(created)
    assert len(created.metadata.fingerprint) == 16
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT ciphertext, encrypted_data_key, wrapping_key_id,
                   request_fingerprint
            FROM router.encrypted_credentials AS credential
            JOIN router.credential_idempotency_bindings AS binding
              ON binding.credential_id = credential.id
            WHERE credential.id = %s
            """,
            (created.metadata.credential_id,),
        ).fetchone()
    assert row is not None
    assert secret.encode() not in bytes(row[0])
    assert secret.encode() not in bytes(row[1])
    assert row[2] == "wrap-1"
    assert len(bytes(row[3])) == 32
    with pytest.raises(CredentialStoreError) as conflict:
        _create(repository, value="different", key="credential-create-key")
    assert conflict.value.code is CredentialStoreErrorCode.IDEMPOTENCY_CONFLICT


def test_encryption_context_wrong_key_and_backup_fail_closed() -> None:
    counter = 0

    def random_bytes(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter]) * size

    cipher = EnvelopeCipher(
        {"wrap-1": WRAPPING_KEY},
        current_key_id="wrap-1",
        random_bytes=random_bytes,
    )
    context = {
        "credential_id": str(uuid.uuid4()),
        "owner_scope": "global",
        "provider_catalog_id": PROVIDER_ID,
    }
    envelope = cipher.encrypt(b"secret", context=context)
    assert bytes(cipher.decrypt(envelope, context=context)) == b"secret"
    with pytest.raises(EnvelopeDecryptionError):
        cipher.decrypt(envelope, context={**context, "owner_scope": SERVICE_ID})
    wrong = EnvelopeCipher(
        {"wrap-1": NEXT_WRAPPING_KEY},
        current_key_id="wrap-1",
        random_bytes=random_bytes,
    )
    with pytest.raises(EnvelopeDecryptionError):
        wrong.decrypt(envelope, context=context)
    absent = EnvelopeCipher(
        {"wrap-2": NEXT_WRAPPING_KEY},
        current_key_id="wrap-2",
        random_bytes=random_bytes,
    )
    with pytest.raises(EnvelopeDecryptionError):
        absent.decrypt(envelope, context=context)


def test_authority_recent_auth_revision_lifecycle_and_audit(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    created = _create(repository).metadata
    with pytest.raises(CredentialStoreError):
        repository.disable(
            replace(_context(), recent_authentication_at=NOW - timedelta(minutes=6)),
            created.credential_id,
            expected_revision=created.revision,
            reason="Stop use",
            now=NOW,
        )
    with pytest.raises(CredentialStoreError) as stale:
        repository.disable(
            _context(),
            created.credential_id,
            expected_revision=str(uuid.uuid4()),
            reason="Stop use",
            now=NOW,
        )
    assert stale.value.code is CredentialStoreErrorCode.STATE_REVISION_CONFLICT
    rotated = repository.replace(
        _context(),
        created.credential_id,
        expected_revision=created.revision,
        reason="Scheduled rotation",
        replacement_secret=SecretInput("new-provider-secret"),
        now=NOW + timedelta(seconds=1),
    )
    disabled = repository.disable(
        _context(),
        created.credential_id,
        expected_revision=rotated.revision,
        reason="Stop use",
        now=NOW + timedelta(seconds=2),
    )
    retired = repository.retire(
        _context(),
        created.credential_id,
        expected_revision=disabled.revision,
        reason="No longer needed",
        now=NOW + timedelta(seconds=3),
    )
    assert retired.state is CredentialState.RETIRED
    with pytest.raises(CredentialStoreError) as terminal:
        repository.replace(
            _context(),
            created.credential_id,
            expected_revision=retired.revision,
            reason="Cannot restore",
            replacement_secret=SecretInput("another-secret"),
            now=NOW + timedelta(seconds=4),
        )
    assert terminal.value.code is CredentialStoreErrorCode.TERMINAL_STATE
    with psycopg.connect(database_url) as connection:
        actions = connection.execute(
            """
            SELECT action, safe_details
            FROM router.audit_events
            WHERE action LIKE 'credential.%'
            ORDER BY occurred_at
            """
        ).fetchall()
        invalidations = connection.execute(
            """
            SELECT action, generation
            FROM router.credential_urgent_invalidations
            ORDER BY sequence
            """
        ).fetchall()
    assert [row[0] for row in actions] == [
        "credential.create",
        "credential.rotate",
        "credential.disable",
        "credential.retire",
    ]
    assert all("top-secret" not in str(row[1]) for row in actions)
    assert invalidations == [("rotate", 2), ("disable", 3), ("retire", 4)]


def test_list_and_service_reference_selection_do_not_expose_secret(
    repository: EncryptedCredentialRepository,
) -> None:
    global_record = _create(repository).metadata
    service_record = _create(
        repository,
        key="service-credential-create",
        owner=CredentialOwner(SERVICE_ID),
    ).metadata
    listed = repository.list_metadata(_context(mutation=False))
    assert {item.credential_id for item in listed} == {
        global_record.credential_id,
        service_record.credential_id,
    }
    selector = replace(
        _context(mutation=False),
        authority_class=AuthorityClass.SERVICE,
        operation="provider_instance.manage",
        scope=Scope(service_id=SERVICE_ID),
    )
    assert repository.reference_is_eligible(
        selector,
        global_record.credential_id,
        service_id=SERVICE_ID,
        provider_catalog_id=PROVIDER_ID,
    )
    assert repository.reference_is_eligible(
        selector,
        service_record.credential_id,
        service_id=SERVICE_ID,
        provider_catalog_id=PROVIDER_ID,
    )
    assert not repository.reference_is_eligible(
        selector,
        service_record.credential_id,
        service_id=SERVICE_ID,
        provider_catalog_id="other-provider",
    )
    other_selector = replace(selector, scope=Scope(service_id=OTHER_SERVICE_ID))
    assert not repository.reference_is_eligible(
        other_selector,
        service_record.credential_id,
        service_id=OTHER_SERVICE_ID,
        provider_catalog_id=PROVIDER_ID,
    )
    with pytest.raises(CredentialStoreError):
        repository.reference_is_eligible(
            selector,
            service_record.credential_id,
            service_id=OTHER_SERVICE_ID,
            provider_catalog_id=PROVIDER_ID,
        )


def test_wrapping_key_staging_and_missing_key_state(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    created = _create(repository).metadata
    rotated_repository = EncryptedCredentialRepository(
        database_url,
        wrapping_keys={"wrap-1": WRAPPING_KEY, "wrap-2": NEXT_WRAPPING_KEY},
        current_wrapping_key_id="wrap-2",
        idempotency_digest_key=DIGEST_KEY,
    )
    assert rotated_repository.rotate_wrapping_key(_context(), now=NOW) == 1
    assert rotated_repository.custody_status().state is WrappingKeyCustodyState.NORMAL
    recovered = EncryptedCredentialRepository(
        database_url,
        wrapping_keys={"wrap-2": NEXT_WRAPPING_KEY},
        current_wrapping_key_id="wrap-2",
        idempotency_digest_key=DIGEST_KEY,
    )
    assert recovered.custody_status().state is WrappingKeyCustodyState.NORMAL
    missing = EncryptedCredentialRepository(
        database_url,
        wrapping_keys={"wrap-3": bytes(range(64, 96))},
        current_wrapping_key_id="wrap-3",
        idempotency_digest_key=DIGEST_KEY,
    )
    status = missing.custody_status()
    assert status.state is WrappingKeyCustodyState.DEGRADED
    assert status.missing_key_ids == ("wrap-2",)
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT generation, current_revision, wrapping_key_id
            FROM router.encrypted_credentials WHERE id = %s
            """,
            (created.credential_id,),
        ).fetchone()
    assert row == (1, uuid.UUID(created.revision), "wrap-2")


def test_route_delivery_isolated_bounded_zeroized_and_urgently_invalidated(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    created = _create(repository).metadata
    _seed_route(database_url, created.credential_id)
    distributor = DataPlaneCredentialDistributor(
        database_url,
        wrapping_keys={"wrap-1": WRAPPING_KEY},
        current_wrapping_key_id="wrap-1",
        active_route_ids=frozenset({ROUTE_ID}),
        maximum_cache_entries=1,
        cache_lifetime=timedelta(minutes=1),
    )
    lease = distributor.secret_for_route(ROUTE_ID, request_id="route-request", now=NOW)
    assert bytes(lease.read(now=NOW)) == b"top-secret-provider-value"
    assert distributor.cache_entry_count == 1
    lease.close()
    with pytest.raises(RuntimeError):
        lease.read(now=NOW)
    with pytest.raises(CredentialStoreError):
        distributor.secret_for_route(
            str(uuid.uuid4()), request_id="wrong-route", now=NOW
        )
    rotated = repository.replace(
        _context(),
        created.credential_id,
        expected_revision=created.revision,
        reason="Urgent rotation",
        replacement_secret=SecretInput("rotated-provider-value"),
        now=NOW + timedelta(seconds=1),
    )
    next_lease = distributor.secret_for_route(
        ROUTE_ID, request_id="route-request-2", now=NOW + timedelta(seconds=1)
    )
    assert next_lease.generation == 2
    assert bytes(next_lease.read(now=NOW + timedelta(seconds=1))) == (
        b"rotated-provider-value"
    )
    assert distributor.invalidation_cursor == 1
    disabled = repository.disable(
        _context(),
        created.credential_id,
        expected_revision=rotated.revision,
        reason="Urgent disable",
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(CredentialStoreError):
        distributor.secret_for_route(
            ROUTE_ID, request_id="route-request-3", now=NOW + timedelta(seconds=2)
        )
    with pytest.raises(RuntimeError):
        next_lease.read(now=NOW + timedelta(seconds=2))
    assert disabled.state is CredentialState.DISABLED
    assert distributor.cache_entry_count == 0
    distributor.close()


def test_durable_invalidation_recovers_after_identity_rollback_gap(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    created = _create(repository).metadata
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "SELECT nextval(pg_get_serial_sequence("
            "'router.credential_urgent_invalidations', 'sequence'))"
        )
        connection.rollback()
    repository.replace(
        _context(),
        created.credential_id,
        expected_revision=created.revision,
        reason="Create one committed event after a rolled-back identity value",
        replacement_secret=SecretInput("rotated-provider-value"),
        now=NOW + timedelta(seconds=1),
    )
    distributor = DataPlaneCredentialDistributor(
        database_url,
        wrapping_keys={"wrap-1": WRAPPING_KEY},
        current_wrapping_key_id="wrap-1",
        active_route_ids=frozenset(),
    )
    applied = distributor.apply_urgent_invalidations()
    assert [item.sequence for item in applied] == [2]
    assert distributor.invalidation_cursor == 2


def test_invalidation_sequences_cannot_commit_out_of_order(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    first = _create(repository).metadata
    second = _create(repository, key="second-ordered-credential").metadata
    first_inserted = threading.Event()
    release_first = threading.Event()

    def insert_first() -> int:
        with (
            psycopg.connect(database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            invalidation = _insert_invalidation(
                connection,
                identity_factory=uuid.uuid4,
                credential_id=uuid.UUID(first.credential_id),
                generation=2,
                action=CredentialAction.ROTATE,
                now=NOW,
            )
            first_inserted.set()
            assert release_first.wait(timeout=5)
        return invalidation.sequence

    def insert_second() -> int:
        assert first_inserted.wait(timeout=5)
        with (
            psycopg.connect(database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            invalidation = _insert_invalidation(
                connection,
                identity_factory=uuid.uuid4,
                credential_id=uuid.UUID(second.credential_id),
                generation=2,
                action=CredentialAction.ROTATE,
                now=NOW,
            )
        return invalidation.sequence

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(insert_first)
        second_future = executor.submit(insert_second)
        assert first_inserted.wait(timeout=5)
        with pytest.raises(concurrent.futures.TimeoutError):
            second_future.result(timeout=0.1)
        release_first.set()
        assert first_future.result(timeout=5) == 1
        assert second_future.result(timeout=5) == 2


def test_route_delivery_rejects_wrong_credential_catalog(
    database_url: str, repository: EncryptedCredentialRepository
) -> None:
    created = repository.create(
        _context(),
        idempotency_key="wrong-catalog-credential",
        owner=CredentialOwner(),
        provider_catalog_id="other-adapter",
        secret=SecretInput("wrong-provider-secret"),
        now=NOW,
    ).metadata
    _seed_route(database_url, created.credential_id)
    distributor = DataPlaneCredentialDistributor(
        database_url,
        wrapping_keys={"wrap-1": WRAPPING_KEY},
        current_wrapping_key_id="wrap-1",
        active_route_ids=frozenset({ROUTE_ID}),
    )
    with pytest.raises(CredentialStoreError) as error:
        distributor.secret_for_route(ROUTE_ID, request_id="route-request", now=NOW)
    assert error.value.code is CredentialStoreErrorCode.NOT_FOUND


def test_cache_eviction_erases_mutable_value() -> None:
    cache = BoundedCredentialCache(maximum_entries=1, lifetime=timedelta(minutes=1))
    first = bytearray(b"first")
    cache.acquire("route-1", now=NOW, loader=lambda: ("credential-1", 1, first)).close()
    cache.acquire(
        "route-2",
        now=NOW,
        loader=lambda: ("credential-2", 1, bytearray(b"second")),
    ).close()
    assert first == bytearray(len(first))
    assert cache.entry_count == 1


def test_route_removal_closes_lease_after_cache_eviction() -> None:
    cache = BoundedCredentialCache(maximum_entries=1, lifetime=timedelta(minutes=1))
    removed_lease = cache.acquire(
        "route-1",
        now=NOW,
        loader=lambda: ("credential-1", 1, bytearray(b"first")),
    )
    cache.acquire(
        "route-2",
        now=NOW,
        loader=lambda: ("credential-2", 1, bytearray(b"second")),
    ).close()
    cache.retain_routes(frozenset({"route-2"}))
    assert removed_lease.closed


def test_concurrent_create_and_change_serialize(
    repository: EncryptedCredentialRepository,
) -> None:
    def create_once(_index: int) -> CredentialResult:
        return _create(repository)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_once, range(2)))
    assert {result.metadata.credential_id for result in results} == {
        results[0].metadata.credential_id
    }
    assert sorted(result.replayed for result in results) == [False, True]
    created = results[0].metadata

    def disable_once(_index: int) -> CredentialMetadata | CredentialStoreError:
        try:
            return repository.disable(
                _context(),
                created.credential_id,
                expected_revision=created.revision,
                reason="Concurrent disable",
                now=NOW + timedelta(seconds=1),
            )
        except CredentialStoreError as error:
            return error

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        changes = list(executor.map(disable_once, range(2)))
    assert sum(not isinstance(item, CredentialStoreError) for item in changes) == 1
    errors = [item for item in changes if isinstance(item, CredentialStoreError)]
    assert len(errors) == 1
    assert errors[0].code is CredentialStoreErrorCode.STATE_REVISION_CONFLICT

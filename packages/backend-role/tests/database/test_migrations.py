"""Forward and rollback migration tests."""

from __future__ import annotations

import concurrent.futures
import uuid

import psycopg
import pytest
from llmrouter_backend.database import applied_versions, migrate, migration_plan

from .helpers import CONFIGURATION_ID, SERVICE_ID, WORKSPACE_ID, seed_scope

_MINIMUM_FOUNDATION_TABLES = 30
_LEGACY_MODEL_ID = "0198a080-0000-7000-8000-000000000085"
_LEGACY_CREDENTIAL_ID = "0198a080-0000-7000-8000-000000000086"
_LEGACY_INSTANCE_ID = "0198a080-0000-7000-8000-000000000087"
_LEGACY_ROUTE_ID = "0198a080-0000-7000-8000-000000000088"
_LEGACY_SOURCE_ID = "0198a080-0000-7000-8000-000000000089"


def _seed_legacy_route_price_source(connection: psycopg.Connection[object]) -> None:
    """Insert one migration-0007 route price source with all legacy values."""
    connection.execute(
        """INSERT INTO router.provider_adapter_types (
               id, settings_schema_name, settings_schema_major, capabilities
           ) VALUES ('provider.legacy', 'provider.settings', 1, '{}')"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models (id, stable_name, capabilities)
           VALUES (%s, 'legacy-model', '{}')""",
        (_LEGACY_MODEL_ID,),
    )
    connection.execute(
        """INSERT INTO router.encrypted_credentials (
               id, owner_kind, credential_kind, ciphertext, encrypted_data_key,
               wrapping_key_id, safe_fingerprint, current_revision,
               last_changed_at
           ) VALUES (%s, 'global', 'provider.legacy', %s, %s, 'wrap', 'safe',
                     %s, now())""",
        (_LEGACY_CREDENTIAL_ID, bytes(32), bytes(32), _LEGACY_CREDENTIAL_ID),
    )
    connection.execute(
        """INSERT INTO router.provider_instances (
               id, owner_kind, adapter_type_id, credential_id, stable_name,
               endpoint_origin, settings_schema_name, settings_schema_major,
               settings
           ) VALUES (%s, 'global', 'provider.legacy', %s, 'legacy-instance',
                     'https://provider.example', 'provider.settings', 1, '{}')""",
        (_LEGACY_INSTANCE_ID, _LEGACY_CREDENTIAL_ID),
    )
    connection.execute(
        """INSERT INTO router.provider_model_routes (
               id, owner_kind, provider_instance_id, canonical_model_id,
               provider_lookup_id, settings_schema_name,
               settings_schema_major, settings
           ) VALUES (%s, 'global', %s, %s, 'legacy-wire',
                     'route.settings', 1, '{}')""",
        (_LEGACY_ROUTE_ID, _LEGACY_INSTANCE_ID, _LEGACY_MODEL_ID),
    )
    connection.execute(
        """INSERT INTO router.route_price_sources (
               id, provider_model_route_id, authority_kind, source_name,
               lookup_identifier, synchronization_schedule, stale_after
           ) VALUES (%s, %s, 'synchronized', 'legacy-source', 'legacy-wire',
                     NULL, interval '9 days')""",
        (_LEGACY_SOURCE_ID, _LEGACY_ROUTE_ID),
    )


def _migrate_current(database_url: str) -> tuple[int, ...]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        return applied_versions(connection)


def test_migration_plan_has_reversible_contiguous_pairs() -> None:
    """Keep each schema change ordered and reversible."""
    plan = migration_plan()
    assert [migration.version for migration in plan] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert all(migration.up_sql and migration.down_sql for migration in plan)


def test_migrate_empty_database(database_url: str) -> None:
    """Create the current schema from an empty database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        assert applied_versions(connection) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
        table_count = connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'router'
            """
        ).fetchone()
        assert table_count is not None
        assert table_count[0] >= _MINIMUM_FOUNDATION_TABLES


def test_upgrade_previous_schema_without_data_loss(database_url: str) -> None:
    """Preserve control data when migration 0003 applies."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=2)
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'kept-service')",
            (SERVICE_ID,),
        )
        migrate(connection)
        assert connection.execute(
            "SELECT stable_name FROM router.services WHERE id = %s", (SERVICE_ID,)
        ).fetchone() == ("kept-service",)


def test_administrator_workspace_backfill_and_rollback_are_data_safe(
    database_url: str,
) -> None:
    """Preserve one legacy grant workspace through migration 0005 and rollback."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=4)
        seed_scope(connection)
        administrator_id = "0198a080-0000-7000-8000-000000000080"
        grant_id = "0198a080-0000-7000-8000-000000000081"
        connection.execute(
            """
            INSERT INTO router.administrators (id, issuer, subject)
            VALUES (%s, 'https://identity.example.test', 'person')
            """,
            (administrator_id,),
        )
        connection.execute(
            """
            INSERT INTO router.administrator_grants (
                id, administrator_id, authority_class, service_id, workspace_id,
                operations
            ) VALUES (%s, %s, 'service', %s, %s, ARRAY['health.read'])
            """,
            (grant_id, administrator_id, SERVICE_ID, WORKSPACE_ID),
        )
        migrate(connection)
        assert connection.execute(
            "SELECT workspace_ids FROM router.administrator_grants WHERE id = %s",
            (grant_id,),
        ).fetchone() == ([uuid.UUID(WORKSPACE_ID)],)
        migrate(connection, target=4)
        assert connection.execute(
            "SELECT workspace_id FROM router.administrator_grants WHERE id = %s",
            (grant_id,),
        ).fetchone() == (uuid.UUID(WORKSPACE_ID),)


def test_administrator_authentication_rollback_rejects_idempotency_loss(
    database_url: str,
) -> None:
    """Stop rollback after a durable administrator grant binding exists."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        administrator_id = "0198a080-0000-7000-8000-000000000082"
        grant_id = "0198a080-0000-7000-8000-000000000083"
        connection.execute(
            """
            INSERT INTO router.administrators (id, issuer, subject)
            VALUES (%s, 'https://identity.example.test', 'person')
            """,
            (administrator_id,),
        )
        connection.execute(
            """
            INSERT INTO router.administrator_grants (
                id, administrator_id, authority_class, operations
            ) VALUES (%s, %s, 'global', ARRAY['grant.manage'])
            """,
            (grant_id, administrator_id),
        )
        connection.execute(
            """
            INSERT INTO router.administrator_grant_idempotency_bindings (
                administrator_id, idempotency_key, request_fingerprint,
                grant_id, created_at
            ) VALUES (%s, 'migration-idempotency-key', %s, %s, now())
            """,
            (administrator_id, bytes(32), grant_id),
        )
        with pytest.raises(psycopg.Error, match="cannot roll back without data loss"):
            migrate(connection, target=4)


def test_credential_store_rollback_rejects_custody_data_loss(
    database_url: str,
) -> None:
    """Stop rollback after an encrypted credential binding exists."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        credential_id = "0198a080-0000-7000-8000-000000000084"
        connection.execute(
            """
            INSERT INTO router.encrypted_credentials (
                id, owner_kind, credential_kind, ciphertext,
                encrypted_data_key, wrapping_key_id, safe_fingerprint,
                current_revision, last_changed_at
            ) VALUES (
                %s, 'global', 'provider.example', %s, %s, 'wrap-1',
                'fingerprint', %s, now()
            )
            """,
            (credential_id, bytes(32), bytes(32), credential_id),
        )
        connection.execute(
            """
            INSERT INTO router.credential_idempotency_bindings (
                actor_id, idempotency_key, request_fingerprint,
                credential_id, created_at
            ) VALUES ('operator', 'credential-create-key', %s, %s, now())
            """,
            (bytes(32), credential_id),
        )
        with pytest.raises(psycopg.Error, match="cannot roll back without data loss"):
            migrate(connection, target=5)


def test_route_price_source_upgrade_and_rollback_preserve_legacy_values(
    database_url: str,
) -> None:
    """Keep all legacy route price authority values through migration 0008."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=7)
        _seed_legacy_route_price_source(connection)
        legacy = ("synchronized", "legacy-source", "legacy-wire", None, 9)
        upgraded = (
            "synchronized",
            "legacy-source",
            "legacy-wire",
            "0 0 * * 0",
            9,
        )
        statement = """
            SELECT authority_kind, source_name, lookup_identifier,
                   synchronization_schedule,
                   extract(day FROM stale_after)::integer
            FROM router.route_price_sources WHERE id = %s
        """
        row = connection.execute(statement, (_LEGACY_SOURCE_ID,)).fetchone()
        assert row == legacy
        migrate(connection)
        row = connection.execute(statement, (_LEGACY_SOURCE_ID,)).fetchone()
        assert row == upgraded
        migrate(connection, target=7)
        row = connection.execute(statement, (_LEGACY_SOURCE_ID,)).fetchone()
        assert row == legacy


def test_admission_upgrade_keeps_legacy_targetless_rows(database_url: str) -> None:
    """Keep legacy rows but require one target for each new admission."""
    row_id = "0198a080-0000-7000-8000-000000000141"
    request_id = "0198a080-0000-7000-8000-000000000142"
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=8)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.logical_requests (
                   row_id, request_id, request_kind, service_id, workspace_id,
                   configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, data_profile, capture_enabled
               ) VALUES (%s, %s, 'model', %s, %s, %s, 1,
                         decode(repeat('14', 32), 'hex'), 'service-data', true)""",
            (
                row_id,
                request_id,
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
            ),
        )
        migrate(connection)
        assert connection.execute(
            """SELECT assignment_id, exact_route_id
               FROM router.logical_requests WHERE row_id = %s""",
            (row_id,),
        ).fetchone() == (None, None)
        connection.execute(
            """UPDATE router.logical_requests
               SET state = 'running', state_revision = 2
               WHERE row_id = %s""",
            (row_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.logical_requests (
                       row_id, request_id, request_kind, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256, data_profile, capture_enabled
                   ) VALUES (
                       '0198a080-0000-7000-8000-000000000143',
                       '0198a080-0000-7000-8000-000000000144', 'model', %s, %s,
                       %s, 1, decode(repeat('15', 32), 'hex'),
                       'service-data', true
                   )""",
                (SERVICE_ID, WORKSPACE_ID, CONFIGURATION_ID),
            )
        migrate(connection, target=8)
        assert connection.execute(
            "SELECT request_id FROM router.logical_requests WHERE row_id = %s",
            (row_id,),
        ).fetchone() == (uuid.UUID(request_id),)


def test_route_price_source_rollback_rejects_new_manual_pin_loss(
    database_url: str,
) -> None:
    """Stop rollback when a new manual pin cannot fit the legacy schema."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=7)
        _seed_legacy_route_price_source(connection)
        migrate(connection)
        connection.execute(
            """UPDATE router.route_price_sources
               SET authority_kind = 'manual', source_name = NULL,
                   lookup_identifier = NULL
               WHERE id = %s""",
            (_LEGACY_SOURCE_ID,),
        )
        with pytest.raises(psycopg.Error, match="cannot roll back without data loss"):
            migrate(connection, target=7)


def test_price_snapshot_rollback_rejects_restored_uniqueness_loss(
    database_url: str,
) -> None:
    """Stop rollback when new source evidence has a legacy duplicate key."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        for fetched_at in ("2026-08-13T10:00:00Z", "2026-08-13T11:00:00Z"):
            connection.execute(
                """INSERT INTO router.price_source_snapshots (
                       id, source_name, fetched_at, content_sha256
                   ) VALUES (gen_random_uuid(), 'catalog-test', %s,
                             decode(repeat('aa', 32), 'hex'))""",
                (fetched_at,),
            )
        with pytest.raises(psycopg.Error, match="cannot roll back without data loss"):
            migrate(connection, target=7)


def test_migration_history_rejects_a_gap(database_url: str) -> None:
    """Reject an applied migration set that is not a contiguous prefix."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "DELETE FROM public.router_schema_migrations WHERE version = 1"
        )
        with pytest.raises(RuntimeError, match="contiguous prefix"):
            applied_versions(connection)


def test_migration_history_rejects_checksum_change(database_url: str) -> None:
    """Reject a changed forward or rollback migration after application."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            """
            UPDATE public.router_schema_migrations
            SET checksum = repeat('0', 64)
            WHERE version = 2
            """
        )
        with pytest.raises(RuntimeError, match="does not match"):
            applied_versions(connection)


def test_concurrent_migration_runners_serialize(database_url: str) -> None:
    """Serialize two runners that apply the same pending migration."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_migrate_current, [database_url, database_url]))
    assert results == [(1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 5, 6, 7, 8, 9)]


def test_rollback_keeps_previous_schema_data(database_url: str) -> None:
    """Remove runtime tables without removing prior control data."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'kept-service')",
            (SERVICE_ID,),
        )
        migrate(connection, target=2)
        assert applied_versions(connection) == (1, 2)
        assert connection.execute(
            "SELECT stable_name FROM router.services WHERE id = %s", (SERVICE_ID,)
        ).fetchone() == ("kept-service",)
        assert connection.execute(
            "SELECT to_regclass('router.logical_requests')"
        ).fetchone() == ("router.logical_requests",)
        assert connection.execute(
            "SELECT to_regclass('router.workspace_lifecycle_operations')"
        ).fetchone() == (None,)
        migrate(connection, target=1)
        assert connection.execute(
            "SELECT to_regclass('router.logical_requests')"
        ).fetchone() == (None,)
        migrate(connection)
        assert applied_versions(connection) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
        assert connection.execute(
            "SELECT stable_name FROM router.services WHERE id = %s", (SERVICE_ID,)
        ).fetchone() == ("kept-service",)
        assert connection.execute(
            "SELECT to_regclass('router.logical_requests')"
        ).fetchone() == ("router.logical_requests",)
        migrate(connection, target=0)
        assert applied_versions(connection) == ()
        assert connection.execute("SELECT to_regnamespace('router')").fetchone() == (
            None,
        )

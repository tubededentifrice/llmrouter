"""Forward and rollback migration tests."""

from __future__ import annotations

import concurrent.futures
import uuid

import psycopg
import pytest
from llmrouter_backend.database import applied_versions, migrate, migration_plan

from .helpers import SERVICE_ID, WORKSPACE_ID, seed_scope

_MINIMUM_FOUNDATION_TABLES = 30


def _migrate_current(database_url: str) -> tuple[int, ...]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        return applied_versions(connection)


def test_migration_plan_has_reversible_contiguous_pairs() -> None:
    """Keep each schema change ordered and reversible."""
    plan = migration_plan()
    assert [migration.version for migration in plan] == [1, 2, 3, 4, 5, 6]
    assert all(migration.up_sql and migration.down_sql for migration in plan)


def test_migrate_empty_database(database_url: str) -> None:
    """Create the current schema from an empty database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        assert applied_versions(connection) == (1, 2, 3, 4, 5, 6)
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
    assert results == [(1, 2, 3, 4, 5, 6), (1, 2, 3, 4, 5, 6)]


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
        assert applied_versions(connection) == (1, 2, 3, 4, 5, 6)
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

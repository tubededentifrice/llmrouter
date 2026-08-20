"""Forward and rollback migration tests."""
# ruff: noqa: FBT001

from __future__ import annotations

import concurrent.futures
import uuid

import psycopg
import pytest
from llmrouter_backend.database import applied_versions, migrate, migration_plan
from psycopg.types.json import Jsonb

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ROUTE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)

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
    assert [migration.version for migration in plan] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
    ]
    assert all(migration.up_sql and migration.down_sql for migration in plan)


def test_migrate_empty_database(database_url: str) -> None:
    """Create the current schema from an empty database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        assert applied_versions(connection) == (
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
        )
        table_count = connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'router'
            """
        ).fetchone()
        assert table_count is not None
        assert table_count[0] >= _MINIMUM_FOUNDATION_TABLES


def test_provider_routing_migration_rolls_back_and_reapplies(database_url: str) -> None:
    """Apply, remove, and apply the provider routing schema without data."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=15)
        migrate(connection, target=14)
        assert applied_versions(connection)[-1] == 14  # noqa: PLR2004
        migrate(connection, target=15)
        assert applied_versions(connection)[-1] == 15  # noqa: PLR2004


def test_administration_api_migration_rolls_back_and_reapplies(
    database_url: str,
) -> None:
    """Apply, remove, and apply the administration idempotency schema."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        migrate(connection, target=15)
        assert connection.execute(
            "SELECT to_regclass('router.configuration_write_idempotency_bindings')"
        ).fetchone() == (None,)
        migrate(connection)
        assert applied_versions(connection)[-1] == 18  # noqa: PLR2004


def test_administration_api_rollback_rejects_idempotency_loss(
    database_url: str,
) -> None:
    """Keep durable configuration replays when rollback would remove them."""
    operation_id = "0198a080-0000-7000-8000-000000000098"
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.audit_events (
                   event_id, audit_class, actor_kind, actor_id, authority_class,
                   action, permission_result, safe_details, occurred_at
               ) VALUES (%s, 'global_administration', 'administrator', 'test',
                         'global_administrator', 'configuration.publish',
                         'permitted', '{}'::jsonb, now())""",
            (operation_id,),
        )
        connection.execute(
            """INSERT INTO router.configuration_write_idempotency_bindings (
                   actor_id, operation, scope_key, idempotency_key,
                   request_fingerprint, resource_id, active_revision,
                   distribution_state, operation_id, created_at
               ) VALUES ('test', 'assignment.manage', %s, 'migration-key-0001',
                         decode(repeat('01', 32), 'hex'), 'configuration', %s,
                         'distributing', %s, now())""",
            (f"workspace:{SERVICE_ID}:{WORKSPACE_ID}", CONFIGURATION_ID, operation_id),
        )
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState, match="data loss"
        ):
            migrate(connection, target=15)
        assert applied_versions(connection)[-1] == 18  # noqa: PLR2004


def test_embed_session_migration_rolls_back_and_reapplies(database_url: str) -> None:
    """Apply, remove, and restore the empty embed-session extension."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        migrate(connection, target=16)
        assert applied_versions(connection)[-1] == 16  # noqa: PLR2004
        migrate(connection)
        assert applied_versions(connection)[-1] == 18  # noqa: PLR2004


def test_routing_success_guard_rolls_back_and_reapplies(database_url: str) -> None:
    """Keep success valid without weakening the non-success guard."""
    marker = "IF NEW.attempt_state <> 'succeeded' AND EXISTS ("
    definition_query = """SELECT pg_get_functiondef(
        'router.validate_routing_candidate_decision()'::regprocedure
    )"""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        upgraded = connection.execute(definition_query).fetchone()
        assert upgraded is not None
        assert marker in upgraded[0]
        migrate(connection, target=17)
        rolled_back = connection.execute(definition_query).fetchone()
        assert rolled_back is not None
        assert marker not in rolled_back[0]
        migrate(connection)
        reapplied = connection.execute(definition_query).fetchone()
        assert reapplied is not None
        assert marker in reapplied[0]


def test_embed_session_migration_upgrades_and_protects_existing_session(
    database_url: str,
) -> None:
    """Upgrade one legacy session and refuse a rollback that would lose its state."""
    session_id = "0198a080-0000-7000-8000-000000000099"
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=16)
        seed_scope(connection)
        connection.execute(
            """
            INSERT INTO router.embed_sessions (
                id, service_id, workspace_ids, host_subject,
                permitted_actions, host_origin, frame_origin,
                bootstrap_token_digest, expires_at, created_at
            ) VALUES (
                %s, %s, ARRAY[%s::uuid], 'host-user',
                ARRAY['configuration.read'], 'https://host.example',
                'https://router.example', decode(repeat('01', 32), 'hex'),
                transaction_timestamp() + interval '5 minutes',
                transaction_timestamp()
            )
            """,
            (session_id, SERVICE_ID, WORKSPACE_ID),
        )
        migrate(connection)
        upgraded = connection.execute(
            """
            SELECT theme_mode, theme_density, theme_corner_style,
                   frame_nonce_digest, session_token_digest
            FROM router.embed_sessions WHERE id = %s
            """,
            (session_id,),
        ).fetchone()
        assert upgraded == ("system", "comfortable", "rounded", None, None)
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState, match="data loss"
        ):
            migrate(connection, target=16)
        assert applied_versions(connection)[-1] == 18  # noqa: PLR2004


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ({}, False),
        ({"provider_status": None}, False),
        (
            {
                "provider_status": None,
                "retry_after_ms": None,
                "detail_code": None,
            },
            True,
        ),
        (
            {
                "provider_status": 200.5,
                "retry_after_ms": None,
                "detail_code": None,
            },
            False,
        ),
        (
            {
                "provider_status": None,
                "retry_after_ms": 10**100,
                "detail_code": None,
            },
            False,
        ),
        (
            {
                "provider_status": None,
                "retry_after_ms": "true",
                "detail_code": None,
            },
            False,
        ),
        (
            {
                "provider_status": None,
                "retry_after_ms": None,
                "detail_code": None,
                "extra": "unsafe",
            },
            False,
        ),
    ],
)
def test_routing_evidence_validator_is_closed(
    database_url: str, value: object, accepted: bool
) -> None:
    """Reject missing, extra, malformed, or unbounded routing evidence."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        row = connection.execute(
            "SELECT router.valid_redacted_routing_evidence(%s::jsonb)",
            (Jsonb(value),),
        ).fetchone()
        assert row == (accepted,)


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
        migrate(connection, target=14)
        assert connection.execute(
            """SELECT assignment_id, exact_route_id
               FROM router.logical_requests WHERE row_id = %s""",
            (row_id,),
        ).fetchone() == (None, None)
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
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="cannot prove the historical route chain",
        ):
            migrate(connection, target=15)
        migrate(connection, target=8)
        assert connection.execute(
            "SELECT request_id FROM router.logical_requests WHERE row_id = %s",
            (row_id,),
        ).fetchone() == (uuid.UUID(request_id),)


def test_attachment_storage_rollback_rejects_encrypted_content_loss(
    database_url: str,
) -> None:
    """Stop rollback while one encrypted attachment payload exists."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        attachment_id = "0198a080-0000-7000-8000-000000000151"
        with connection.transaction():
            connection.execute(
                """INSERT INTO router.attachments (
                       id, service_id, workspace_id, media_type, byte_length,
                       content_sha256, object_manifest_id, expires_at
                   ) VALUES (%s, %s, %s, 'text/plain', 1,
                             decode(repeat('15', 32), 'hex'), %s,
                             now() + interval '1 day')""",
                (attachment_id, SERVICE_ID, WORKSPACE_ID, attachment_id),
            )
            connection.execute(
                """INSERT INTO router.attachment_status (attachment_id, state)
                   VALUES (%s, 'pending')""",
                (attachment_id,),
            )
            connection.execute(
                """INSERT INTO router.attachment_content (
                       attachment_id, ciphertext, encrypted_data_key, wrapping_key_id
                   ) VALUES (%s, %s, %s, 'test-key')""",
                (attachment_id, bytes(41), bytes(72)),
            )
            connection.execute(
                """UPDATE router.attachment_status
                   SET state = 'ready', revision = 2,
                       verified_at = now(), updated_at = now()
                   WHERE attachment_id = %s""",
                (attachment_id,),
            )
        with pytest.raises(psycopg.Error, match="cannot roll back without data loss"):
            migrate(connection, target=9)


def test_attachment_storage_upgrade_fails_closed_for_legacy_ready_metadata(
    database_url: str,
) -> None:
    """Do not leave legacy ready metadata without encrypted content admissible."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=9)
        seed_scope(connection)
        attachment_id = "0198a080-0000-7000-8000-000000000152"
        verified_at = "2026-08-13T19:00:00Z"
        updated_at = "2026-08-13T19:30:00Z"
        connection.execute(
            """INSERT INTO router.attachments (
                   id, service_id, workspace_id, media_type, byte_length,
                   content_sha256, object_manifest_id, expires_at
               ) VALUES (%s, %s, %s, 'text/plain', 1,
                         decode(repeat('16', 32), 'hex'), %s,
                         now() + interval '1 day')""",
            (attachment_id, SERVICE_ID, WORKSPACE_ID, attachment_id),
        )
        connection.execute(
            """INSERT INTO router.attachment_status (
                   attachment_id, state, revision, verified_at, updated_at
               ) VALUES (%s, 'ready', 7, %s, %s)""",
            (attachment_id, verified_at, updated_at),
        )
        migrate(connection)
        assert connection.execute(
            """SELECT state, verified_at FROM router.attachment_status
               WHERE attachment_id = %s""",
            (attachment_id,),
        ).fetchone() == ("failed", None)
        migrate(connection, target=9)
        restored = connection.execute(
            """SELECT state, revision, verified_at::text, updated_at::text
               FROM router.attachment_status WHERE attachment_id = %s""",
            (attachment_id,),
        ).fetchone()
        assert restored == (
            "ready",
            7,
            "2026-08-13 19:00:00+00",
            "2026-08-13 19:30:00+00",
        )


def test_attachment_storage_rollback_does_not_resurrect_expired_legacy_row(
    database_url: str,
) -> None:
    """Preserve an expiry that occurs after the fail-closed upgrade."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=9)
        seed_scope(connection)
        attachment_id = "0198a080-0000-7000-8000-000000000153"
        connection.execute(
            """INSERT INTO router.attachments (
                   id, service_id, workspace_id, media_type, byte_length,
                   content_sha256, object_manifest_id, expires_at
               ) VALUES (%s, %s, %s, 'text/plain', 1,
                         decode(repeat('17', 32), 'hex'), %s,
                         now() + interval '1 day')""",
            (attachment_id, SERVICE_ID, WORKSPACE_ID, attachment_id),
        )
        connection.execute(
            """INSERT INTO router.attachment_status (
                   attachment_id, state, revision, verified_at
               ) VALUES (%s, 'ready', 4, now())""",
            (attachment_id,),
        )
        migrate(connection)
        connection.execute(
            """UPDATE router.attachment_status
               SET state = 'expired', revision = 6, updated_at = now()
               WHERE attachment_id = %s""",
            (attachment_id,),
        )
        migrate(connection, target=9)
        assert connection.execute(
            """SELECT state, revision FROM router.attachment_status
               WHERE attachment_id = %s""",
            (attachment_id,),
        ).fetchone() == ("expired", 6)


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
    assert results == [
        (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18),
        (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18),
    ]


def test_content_retention_rollback_rejects_configuration_loss(
    database_url: str,
) -> None:
    """Keep capture configuration when the old schema cannot represent it."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """
            INSERT INTO router.capture_policies (
                id, scope_kind, service_id, policy, revision, effective_at
            ) VALUES (%s, 'service', %s, 'complete', 1, transaction_timestamp())
            """,
            (uuid.uuid4(), SERVICE_ID),
        )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="content lifecycle data loss",
        ):
            migrate(connection, target=12)
        assert applied_versions(connection)[-1] == migration_plan()[-1].version
        connection.execute(
            "DELETE FROM router.capture_policies WHERE service_id = %s",
            (SERVICE_ID,),
        )
        connection.execute(
            """
            UPDATE router.retention_limits
            SET maximum_days = 30, revision = 2
            WHERE data_class = 'diagnostic_logs'
            """
        )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="content lifecycle data loss",
        ):
            migrate(connection, target=12)
        assert applied_versions(connection)[-1] == migration_plan()[-1].version


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
        assert applied_versions(connection) == (
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
        )
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


def test_execution_lifecycle_rollback_is_exact_and_reapplies(
    database_url: str,
) -> None:
    """Remove all migration 0014 objects and apply the migration again."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=13)
        migrate(connection, target=14)
        assert connection.execute(
            "SELECT to_regclass('router.execution_stream_events')"
        ).fetchone() == ("router.execution_stream_events",)
        migrate(connection, target=13)
        assert connection.execute(
            """SELECT to_regclass('router.execution_stream_events'),
                      to_regclass('router.execution_cancellations'),
                      to_regclass('router.execution_cancellation_audit')"""
        ).fetchone() == (None, None, None)
        assert connection.execute(
            """SELECT count(*) FROM information_schema.columns
               WHERE table_schema = 'router' AND table_name = 'agent_runs'
                 AND column_name IN (
                     'status_location','cancel_location','events_location',
                     'safe_error','expires_at','capture_enabled','capture_reason'
                 )"""
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pg_extension WHERE extname = 'pgcrypto'"
        ).fetchone() == (0,)
        migrate(connection, target=14)
        assert applied_versions(connection)[-1] == 14  # noqa: PLR2004


def test_execution_lifecycle_old_run_can_roll_back(database_url: str) -> None:
    """Keep a conservative marker for a run that existed before migration 0014."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=13)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.agent_runs (
                   row_id, run_id, service_id, workspace_id,
                   configuration_revision_id, fingerprint_version,
                   fingerprint_sha256
               ) VALUES (%s, %s, %s, %s, %s, 1, %s)""",
            (
                "0198a080-0000-7000-8000-000000000090",
                "0198a080-0000-7000-8000-000000000091",
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
                bytes.fromhex("90" * 32),
            ),
        )
        migrate(connection, target=14)
        migrate(connection, target=13)
        assert applied_versions(connection)[-1] == 13  # noqa: PLR2004


@pytest.mark.parametrize("legacy_work", ["provider_attempt", "run_lease"])
def test_execution_lifecycle_rejects_unjournaled_legacy_work(
    database_url: str, legacy_work: str
) -> None:
    """Reject active identities that have no pre-0014 execution journal."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=13)
        seed_scope(connection)
        if legacy_work == "provider_attempt":
            insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
            price_version_id = "0198a080-0000-7000-8000-000000000094"
            connection.execute(
                """INSERT INTO router.route_price_versions (
                       id, provider_model_route_id, version_number,
                       currency, status
                   ) VALUES (%s, %s, 1, 'USD', 'current')""",
                (price_version_id, FIXTURE_ROUTE_ID),
            )
            connection.execute(
                """INSERT INTO router.provider_attempts (
                       id, request_row_id, service_id, workspace_id,
                       attempt_number, provider_model_route_id, route_generation,
                       assignment_revision_id, price_version_id, state, finished_at
                   ) VALUES (
                       %s, %s, %s, %s, 1, %s,
                       (SELECT generation FROM router.provider_model_routes
                        WHERE id = %s),
                       %s, %s, 'failed', transaction_timestamp()
                   )""",
                (
                    "0198a080-0000-7000-8000-000000000095",
                    REQUEST_ROW_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    FIXTURE_ROUTE_ID,
                    FIXTURE_ROUTE_ID,
                    CONFIGURATION_ID,
                    price_version_id,
                ),
            )
        else:
            run_row_id = "0198a080-0000-7000-8000-000000000096"
            connection.execute(
                """INSERT INTO router.agent_runs (
                       row_id, run_id, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256
                   ) VALUES (%s, %s, %s, %s, %s, 1, %s)""",
                (
                    run_row_id,
                    "0198a080-0000-7000-8000-000000000097",
                    SERVICE_ID,
                    WORKSPACE_ID,
                    CONFIGURATION_ID,
                    bytes.fromhex("96" * 32),
                ),
            )
            connection.execute(
                """INSERT INTO router.control_epochs (epoch, fencing_evidence)
                   VALUES (1, 'legacy-test')"""
            )
            connection.execute(
                """INSERT INTO router.run_leases (
                       run_row_id, owner_node_id, control_epoch, owner_epoch,
                       lease_generation, expires_at
                   ) VALUES (%s, %s, 1, 1, 1,
                             transaction_timestamp() + interval '1 hour')""",
                (run_row_id, "0198a080-0000-7000-8000-000000000098"),
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="non-admitted execution data",
        ):
            migrate(connection, target=14)
        assert applied_versions(connection)[-1] == 13  # noqa: PLR2004


def test_execution_lifecycle_new_run_blocks_lossy_rollback(database_url: str) -> None:
    """Refuse to discard the explicit capture decision of a new agent run."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.agent_runs (
                   row_id, run_id, service_id, workspace_id,
                   configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, capture_enabled, capture_reason
               ) VALUES (%s, %s, %s, %s, %s, 1, %s, false, 'configured')""",
            (
                "0198a080-0000-7000-8000-000000000092",
                "0198a080-0000-7000-8000-000000000093",
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
                bytes.fromhex("91" * 32),
            ),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="data loss"):
            migrate(connection, target=13)
        assert applied_versions(connection)[-1] == 18  # noqa: PLR2004

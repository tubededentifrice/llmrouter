"""Database test data with no private or runtime content."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from psycopg import Connection

SERVICE_ID = "0198a080-0000-7000-8000-000000000001"
OTHER_SERVICE_ID = "0198a080-0000-7000-8000-000000000002"
WORKSPACE_ID = "0198a080-0000-7000-8000-000000000003"
CONFIGURATION_ID = "0198a080-0000-7000-8000-000000000004"
REQUEST_ROW_ID = "0198a080-0000-7000-8000-000000000005"
REQUEST_ID = "0198a080-0000-7000-8000-000000000006"
OTHER_WORKSPACE_ID = "0198a080-0000-7000-8000-000000000007"
FIXTURE_CREDENTIAL_ID = "0198a080-0000-7000-8000-000000000008"
FIXTURE_INSTANCE_ID = "0198a080-0000-7000-8000-000000000009"
FIXTURE_MODEL_ID = "0198a080-0000-7000-8000-000000000010"
FIXTURE_ROUTE_ID = "0198a080-0000-7000-8000-000000000011"
FIXTURE_ASSIGNMENT_ID = "0198a080-0000-7000-8000-000000000012"


def seed_scope(connection: Connection[Any]) -> None:
    """Insert one service, workspace, and immutable configuration revision."""
    connection.execute(
        """
        INSERT INTO router.services (id, stable_name)
        VALUES (%s, 'service-a'), (%s, 'service-b')
        """,
        (SERVICE_ID, OTHER_SERVICE_ID),
    )
    connection.execute(
        """
        INSERT INTO router.workspaces (
            id, service_id, caller_reference, creation_idempotency_key,
            creation_fingerprint
        ) VALUES (%s, %s, 'caller-a', 'create-a', decode(repeat('01', 32), 'hex'))
        """,
        (WORKSPACE_ID, SERVICE_ID),
    )
    connection.execute(
        """
        INSERT INTO router.workspaces (
            id, service_id, caller_reference, creation_idempotency_key,
            creation_fingerprint
        ) VALUES (%s, %s, 'caller-b', 'create-b', decode(repeat('04', 32), 'hex'))
        """,
        (OTHER_WORKSPACE_ID, OTHER_SERVICE_ID),
    )
    connection.execute(
        """
        INSERT INTO router.configuration_revisions (
            id, scope_kind, service_id, workspace_id, revision_number,
            content, content_sha256, created_by_kind, created_by_id
        ) VALUES (
            %s, 'workspace', %s, %s, 1, '{}'::jsonb,
            decode(repeat('02', 32), 'hex'), 'system', 'test'
        )
        """,
        (CONFIGURATION_ID, SERVICE_ID, WORKSPACE_ID),
    )


def seed_request_target(connection: Connection[Any]) -> None:
    """Insert one valid assignment for current-schema request fixtures."""
    exists = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM router.assignment_definitions WHERE id = %s)",
        (FIXTURE_ASSIGNMENT_ID,),
    ).fetchone()
    if exists == (True,):
        return
    connection.execute(
        """INSERT INTO router.provider_adapter_types (
               id, settings_schema_name, settings_schema_major, capabilities
           ) VALUES ('provider.fixture', 'provider.settings', 1, '{}')"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models (id, stable_name, capabilities)
           VALUES (%s, 'fixture-model', '{}')""",
        (FIXTURE_MODEL_ID,),
    )
    connection.execute(
        """INSERT INTO router.encrypted_credentials (
               id, owner_kind, credential_kind, ciphertext, encrypted_data_key,
               wrapping_key_id, safe_fingerprint, current_revision,
               last_changed_at
           ) VALUES (%s, 'global', 'provider.fixture', %s, %s, 'wrap', 'fixture',
                     %s, now())""",
        (FIXTURE_CREDENTIAL_ID, bytes(32), bytes(32), CONFIGURATION_ID),
    )
    connection.execute(
        """INSERT INTO router.provider_instances (
               id, owner_kind, adapter_type_id, credential_id, stable_name,
               endpoint_origin, settings_schema_name, settings_schema_major,
               settings, current_revision
           ) VALUES (%s, 'global', 'provider.fixture', %s, 'fixture-instance',
                     'https://provider.example', 'provider.settings', 1, '{}', %s)""",
        (FIXTURE_INSTANCE_ID, FIXTURE_CREDENTIAL_ID, CONFIGURATION_ID),
    )
    connection.execute(
        """INSERT INTO router.provider_model_routes (
               id, owner_kind, provider_instance_id, canonical_model_id,
               provider_lookup_id, settings_schema_name, settings_schema_major,
               settings, current_revision, wire_model
           ) VALUES (%s, 'global', %s, %s, 'fixture-wire', 'route.settings', 1,
                     '{}', %s, 'fixture-wire')""",
        (FIXTURE_ROUTE_ID, FIXTURE_INSTANCE_ID, FIXTURE_MODEL_ID, CONFIGURATION_ID),
    )
    insert_assignment(connection, FIXTURE_ASSIGNMENT_ID, CONFIGURATION_ID)


def insert_assignment(
    connection: Connection[Any], assignment_id: str, configuration_id: str
) -> None:
    """Insert one assignment that uses the shared fixture route."""
    transaction = connection.transaction() if connection.autocommit else nullcontext()
    with transaction:
        connection.execute(
            """INSERT INTO router.assignment_definitions (
                   id, configuration_revision_id, stable_name
               ) VALUES (%s, %s, %s)""",
            (assignment_id, configuration_id, f"fixture-{assignment_id[-6:]}"),
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates (
                   assignment_id, configuration_revision_id, ordinal,
                   provider_model_route_id, attempt_timeout_seconds,
                   attempt_timeout_ms
               ) VALUES (%s, %s, 1, %s, 30, 30000)""",
            (assignment_id, configuration_id, FIXTURE_ROUTE_ID),
        )


def insert_request(connection: Connection[Any], row_id: str, request_id: str) -> None:
    """Insert one admitted model request."""
    seed_request_target(connection)
    connection.execute(
        """
        INSERT INTO router.logical_requests (
            row_id, request_id, request_kind, service_id, workspace_id,
            assignment_id, configuration_revision_id, fingerprint_version,
            fingerprint_sha256, data_profile, capture_enabled
        ) VALUES (
            %s, %s, 'model', %s, %s, %s, %s, 1,
            decode(repeat('03', 32), 'hex'), 'service-data', true
        )
        """,
        (
            row_id,
            request_id,
            SERVICE_ID,
            WORKSPACE_ID,
            FIXTURE_ASSIGNMENT_ID,
            CONFIGURATION_ID,
        ),
    )

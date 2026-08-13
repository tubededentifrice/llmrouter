"""Database test data with no private or runtime content."""

from __future__ import annotations

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


def insert_request(connection: Connection[Any], row_id: str, request_id: str) -> None:
    """Insert one admitted model request."""
    connection.execute(
        """
        INSERT INTO router.logical_requests (
            row_id, request_id, request_kind, service_id, workspace_id,
            configuration_revision_id, fingerprint_version, fingerprint_sha256,
            data_profile, capture_enabled
        ) VALUES (
            %s, %s, 'model', %s, %s, %s, 1,
            decode(repeat('03', 32), 'hex'), 'service-data', true
        )
        """,
        (row_id, request_id, SERVICE_ID, WORKSPACE_ID, CONFIGURATION_ID),
    )

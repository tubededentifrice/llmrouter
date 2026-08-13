"""Control-plane scope and immutability constraints."""

from __future__ import annotations

import psycopg
import pytest
from llmrouter_backend.database import migrate

from .helpers import (
    CONFIGURATION_ID,
    OTHER_SERVICE_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    seed_scope,
)


def test_service_parent_cycle_fails_at_commit(database_url: str) -> None:
    """Reject a cycle in the one-parent service chain."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'a'), (%s, 'b')",
            (SERVICE_ID, OTHER_SERVICE_ID),
        )
        connection.execute(
            """
            UPDATE router.services
            SET parent_service_id = %s, state_revision = 2
            WHERE id = %s
            """,
            (OTHER_SERVICE_ID, SERVICE_ID),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE router.services
                SET parent_service_id = %s, state_revision = 2
                WHERE id = %s
                """,
                (SERVICE_ID, OTHER_SERVICE_ID),
            )


def test_workspace_dual_idempotency_keys_are_unique(database_url: str) -> None:
    """Reject reuse of either workspace creation identity."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "INSERT INTO router.services (id, stable_name) VALUES (%s, 'a')",
            (SERVICE_ID,),
        )
        connection.execute(
            """
            INSERT INTO router.workspaces (
                id, service_id, caller_reference, creation_idempotency_key,
                creation_fingerprint
            ) VALUES (%s, %s, 'caller', 'key', decode(repeat('01', 32), 'hex'))
            """,
            (WORKSPACE_ID, SERVICE_ID),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO router.workspaces (
                    id, service_id, caller_reference, creation_idempotency_key,
                    creation_fingerprint
                ) VALUES (
                    '0198a080-0000-7000-8000-000000000099', %s, 'caller', 'other',
                    decode(repeat('02', 32), 'hex')
                )
                """,
                (SERVICE_ID,),
            )


def test_workspace_creation_identity_is_immutable(database_url: str) -> None:
    """Reject changes to the durable workspace creation binding."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.workspaces
                SET caller_reference = 'changed', state_revision = 2
                WHERE id = %s
                """,
                (WORKSPACE_ID,),
            )


def test_configuration_revisions_are_contiguous(database_url: str) -> None:
    """Reject a skipped configuration revision in one scope."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                """
                INSERT INTO router.configuration_revisions (
                    id, scope_kind, service_id, workspace_id, revision_number,
                    content, content_sha256, created_by_kind, created_by_id
                ) VALUES (
                    '0198a080-0000-7000-8000-000000000098', 'workspace', %s, %s,
                    3, '{}'::jsonb, decode(repeat('08', 32), 'hex'), 'system', 'test'
                )
                """,
                (SERVICE_ID, WORKSPACE_ID),
            )


def test_configuration_revision_is_append_only(database_url: str) -> None:
    """Reject mutation of an immutable configuration revision."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.configuration_revisions
                SET content = '{"changed": true}'
                WHERE id = %s
                """,
                (CONFIGURATION_ID,),
            )


def test_assignment_requires_nonempty_chain(database_url: str) -> None:
    """Reject an assignment with no candidate at transaction commit."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO router.assignment_definitions (
                    id, configuration_revision_id, stable_name
                ) VALUES ('0198a080-0000-7000-8000-000000000020', %s, 'default')
                """,
                (CONFIGURATION_ID,),
            )


def test_active_configuration_must_match_revision_scope(database_url: str) -> None:
    """Reject an active pointer for a different scope."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO router.active_configurations (
                    scope_kind, service_id, revision_id, revision_number
                ) VALUES ('service', %s, %s, 1)
                """,
                (SERVICE_ID, CONFIGURATION_ID),
            )

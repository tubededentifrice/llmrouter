"""Runtime concurrency, fencing, ledger, and query-plan tests."""

from __future__ import annotations

import concurrent.futures
import uuid
from decimal import Decimal

import psycopg
import pytest
from llmrouter_backend.database import migrate

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ASSIGNMENT_ID,
    OTHER_SERVICE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_request_target,
    seed_scope,
)


def _admit(database_url: str, row_id: str) -> str:
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            insert_request(connection, row_id, REQUEST_ID)
    except psycopg.errors.UniqueViolation:
        return "duplicate"
    return "created"


def test_concurrent_admission_creates_one_binding(database_url: str) -> None:
    """Serialize duplicate admission across two database connections."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda row_id: _admit(database_url, row_id),
                [str(uuid.uuid4()), str(uuid.uuid4())],
            )
        )
    assert sorted(results) == ["created", "duplicate"]


def test_terminal_request_state_cannot_change(database_url: str) -> None:
    """Reject a transition away from a terminal request state."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            """
            UPDATE router.logical_requests
            SET state = 'running', state_revision = 2
            WHERE row_id = %s
            """,
            (REQUEST_ROW_ID,),
        )
        connection.execute(
            """
            UPDATE router.logical_requests
            SET state = 'succeeded', state_revision = 3,
                terminal_at = transaction_timestamp(),
                expires_at = transaction_timestamp() + interval '24 hours'
            WHERE row_id = %s
            """,
            (REQUEST_ROW_ID,),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.logical_requests
                SET state = 'failed', state_revision = 4,
                    terminal_at = transaction_timestamp()
                WHERE row_id = %s
                """,
                (REQUEST_ROW_ID,),
            )


def test_attachment_join_rejects_cross_service_scope(database_url: str) -> None:
    """Reject an attachment from a different service scope."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        attachment_id = "0198a080-0000-7000-8000-000000000030"
        connection.execute(
            """
            INSERT INTO router.attachments (
                id, service_id, media_type, byte_length, content_sha256,
                object_manifest_id, expires_at
            ) VALUES (
                %s, %s, 'text/plain', 10, decode(repeat('04', 32), 'hex'),
                '0198a080-0000-7000-8000-000000000031',
                transaction_timestamp() + interval '7 days'
            )
            """,
            (attachment_id, OTHER_SERVICE_ID),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO router.request_attachments (
                    request_row_id, service_id, workspace_id, attachment_id,
                    ordinal, content_sha256, byte_length
                ) VALUES (
                    %s, %s, %s, %s, 1, decode(repeat('04', 32), 'hex'), 10
                )
                """,
                (REQUEST_ROW_ID, SERVICE_ID, WORKSPACE_ID, attachment_id),
            )


def test_request_configuration_rejects_cross_service_scope(database_url: str) -> None:
    """Reject a configuration revision from another service scope."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        seed_request_target(connection)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO router.logical_requests (
                    row_id, request_id, request_kind, service_id,
                    assignment_id, configuration_revision_id, fingerprint_version,
                    fingerprint_sha256, data_profile, capture_enabled
                ) VALUES (
                    '0198a080-0000-7000-8000-000000000091',
                    '0198a080-0000-7000-8000-000000000092', 'model', %s, %s, %s, 1,
                    decode(repeat('09', 32), 'hex'), 'service-data', true
                )
                """,
                (OTHER_SERVICE_ID, FIXTURE_ASSIGNMENT_ID, CONFIGURATION_ID),
            )


def test_run_lease_generation_must_increase(database_url: str) -> None:
    """Fence an earlier run owner generation."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        run_row_id = "0198a080-0000-7000-8000-000000000040"
        connection.execute(
            """
            INSERT INTO router.agent_runs (
                row_id, run_id, service_id, workspace_id,
                configuration_revision_id, fingerprint_version,
                fingerprint_sha256
            ) VALUES (
                %s, '0198a080-0000-7000-8000-000000000041', %s, %s, %s, 1,
                decode(repeat('05', 32), 'hex')
            )
            """,
            (run_row_id, SERVICE_ID, WORKSPACE_ID, CONFIGURATION_ID),
        )
        connection.execute(
            """
            INSERT INTO router.control_epochs (epoch, fencing_evidence)
            VALUES (1, 'test')
            """
        )
        connection.execute(
            """
            INSERT INTO router.run_leases (
                run_row_id, owner_node_id, control_epoch, owner_epoch,
                lease_generation, expires_at
            ) VALUES (
                %s, '0198a080-0000-7000-8000-000000000042', 1, 1, 1,
                transaction_timestamp() + interval '1 minute'
            )
            """,
            (run_row_id,),
        )
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                """
                UPDATE router.run_leases
                SET owner_node_id = '0198a080-0000-7000-8000-000000000043',
                    lease_generation = 1
                WHERE run_row_id = %s
                """,
                (run_row_id,),
            )


def test_stale_run_owner_cannot_resolve_effect(database_url: str) -> None:
    """Fence an effect resolution after another run owner takes over."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        run_row_id = "0198a080-0000-7000-8000-000000000060"
        connection.execute(
            """
            INSERT INTO router.agent_runs (
                row_id, run_id, service_id, workspace_id,
                configuration_revision_id, fingerprint_version,
                fingerprint_sha256
            ) VALUES (
                %s, '0198a080-0000-7000-8000-000000000061', %s, %s, %s, 1,
                decode(repeat('0a', 32), 'hex')
            )
            """,
            (run_row_id, SERVICE_ID, WORKSPACE_ID, CONFIGURATION_ID),
        )
        connection.execute(
            """
            INSERT INTO router.control_epochs (epoch, fencing_evidence)
            VALUES (1, 'test')
            """
        )
        connection.execute(
            """
            INSERT INTO router.run_leases (
                run_row_id, owner_node_id, control_epoch, owner_epoch,
                lease_generation, expires_at
            ) VALUES (
                %s, '0198a080-0000-7000-8000-000000000062', 1, 1, 1,
                transaction_timestamp() + interval '1 minute'
            )
            """,
            (run_row_id,),
        )
        effect_id = "0198a080-0000-7000-8000-000000000063"
        connection.execute(
            """
            INSERT INTO router.effect_intents (
                id, run_row_id, owner_epoch, operation_identity, effect_kind,
                request_fingerprint, state
            ) VALUES (
                %s, %s, 1, 'operation-1', 'business-tool',
                decode(repeat('0b', 32), 'hex'), 'intent'
            )
            """,
            (effect_id, run_row_id),
        )
        connection.execute(
            """
            UPDATE router.run_leases
            SET owner_node_id = '0198a080-0000-7000-8000-000000000064',
                owner_epoch = 2, lease_generation = 2
            WHERE run_row_id = %s
            """,
            (run_row_id,),
        )
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                """
                UPDATE router.effect_intents
                SET state = 'confirmed', resolved_at = transaction_timestamp()
                WHERE id = %s
                """,
                (effect_id,),
            )


def test_budget_allowances_are_bounded_and_fenced(database_url: str) -> None:
    """Preserve a legacy allowance and make its generation immutable."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=10)
        budget_id = "0198a080-0000-7000-8000-000000000070"
        allowance_id = "0198a080-0000-7000-8000-000000000071"
        connection.execute(
            """
            INSERT INTO router.budget_scopes (
                id, scope_kind, currency, hard_limit
            ) VALUES (%s, 'global', 'USD', 10.000000000000000000)
            """,
            (budget_id,),
        )
        connection.execute(
            """
            INSERT INTO router.budget_allowance_leases (
                id, budget_scope_id, currency, owner_node_id, lease_generation,
                issued_amount, expires_at, safety_until
            ) VALUES (
                %s, %s, 'USD', '0198a080-0000-7000-8000-000000000072', 1,
                6.000000000000000000,
                transaction_timestamp() + interval '1 minute',
                transaction_timestamp() + interval '2 minutes'
            )
            """,
            (allowance_id, budget_id),
        )
        migrate(connection)
        assert connection.execute(
            """SELECT batch_id = id, maximum_correction_risk
               FROM router.budget_allowance_leases WHERE id = %s""",
            (allowance_id,),
        ).fetchone() == (True, Decimal(0))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.budget_allowance_leases
                SET lease_generation = 2 WHERE id = %s
                """,
                (allowance_id,),
            )


def test_worker_job_owner_and_terminal_state_are_fenced(database_url: str) -> None:
    """Reject a stale worker owner and a terminal job mutation."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        job_id = "0198a080-0000-7000-8000-000000000080"
        owner_id = "0198a080-0000-7000-8000-000000000081"
        connection.execute(
            """
            INSERT INTO router.worker_jobs (id, job_kind, scope_key, payload)
            VALUES (%s, 'test', 'scope', '{}'::jsonb)
            """,
            (job_id,),
        )
        connection.execute(
            """
            UPDATE router.worker_jobs
            SET state = 'running', owner_node_id = %s, lease_generation = 2,
                lease_expires_at = transaction_timestamp() + interval '1 minute'
            WHERE id = %s
            """,
            (owner_id, job_id),
        )
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                """
                UPDATE router.worker_jobs
                SET state = 'succeeded',
                    owner_node_id = '0198a080-0000-7000-8000-000000000082',
                    lease_generation = 3, lease_expires_at = NULL
                WHERE id = %s
                """,
                (job_id,),
            )
        connection.execute(
            """
            UPDATE router.worker_jobs
            SET state = 'succeeded', lease_generation = 3,
                lease_expires_at = NULL
            WHERE id = %s
            """,
            (job_id,),
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.worker_jobs
                SET priority = 2, lease_generation = 4
                WHERE id = %s
                """,
                (job_id,),
            )


def test_append_only_event_and_due_query_index(database_url: str) -> None:
    """Protect canonical events and use the due-worker partial index."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            """
            INSERT INTO router.canonical_events (
                event_id, source_node_id, source_sequence, event_class,
                payload_sha256, durable_replay_position, occurred_at
            ) VALUES (
                '0198a080-0000-7000-8000-000000000050',
                '0198a080-0000-7000-8000-000000000051', 1, 'audit',
                decode(repeat('06', 32), 'hex'), 'position-1', transaction_timestamp()
            )
            """
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE router.canonical_events
                SET durable_replay_position = 'changed'
                WHERE event_id = '0198a080-0000-7000-8000-000000000050'
                """
            )

        connection.execute("SET enable_seqscan = off")
        plan = connection.execute(
            """
            EXPLAIN (COSTS OFF)
            SELECT id FROM router.worker_jobs
            WHERE state IN ('ready', 'retry_wait')
              AND available_at <= transaction_timestamp()
            ORDER BY priority DESC, available_at, id
            LIMIT 1
            """
        ).fetchall()
        assert "worker_jobs_due_idx" in "\n".join(row[0] for row in plan)

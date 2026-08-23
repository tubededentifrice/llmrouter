"""PostgreSQL model-request view tests."""
# ruff: noqa: D103

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from llmrouter_backend.admission.repository import _snapshot_routing_chain
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
    ServicePrincipal,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
)
from llmrouter_backend.model_requests.repository import PostgresModelRequestViews
from psycopg.rows import dict_row

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ASSIGNMENT_ID,
    FIXTURE_INSTANCE_ID,
    FIXTURE_MODEL_ID,
    FIXTURE_ROUTE_ID,
    OTHER_SERVICE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
FALLBACK_ROUTE_ID = "0198a080-0000-7000-8000-000000000021"
PRIMARY_PRICE_ID = "0198a080-0000-7000-8000-000000000022"
FALLBACK_PRICE_ID = "0198a080-0000-7000-8000-000000000023"
FAILED_ATTEMPT_ID = "0198a080-0000-7000-8000-000000000024"
SUCCEEDED_ATTEMPT_ID = "0198a080-0000-7000-8000-000000000025"


def _principal(
    service_id: str = SERVICE_ID,
    allowed_workspaces: frozenset[str] | None = frozenset({WORKSPACE_ID}),
) -> ServicePrincipal:
    return ServicePrincipal(
        "test",
        "token-id",
        Audience.DATA_PLANE,
        service_id,
        frozenset({"model.read", "model.cancel"}),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=4),
        1,
        allowed_workspaces,
    )


def _context(operation: str = "model.read") -> RequestContext:
    return RequestContext(
        "transport-request",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.DATA_PLANE,
        operation,
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        operation in {"model.create", "model.cancel"},
    )


def test_views_hide_other_scopes_and_return_bounded_zero_accounting(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)

    views = PostgresModelRequestViews(database_url)
    assert views.resolve_scope(_principal(), REQUEST_ID) == Scope(
        SERVICE_ID, WORKSPACE_ID
    )
    assert views.resolve_scope(_principal(OTHER_SERVICE_ID, None), REQUEST_ID) is None
    assert views.resolve_scope(_principal(SERVICE_ID, frozenset()), REQUEST_ID) is None

    status = views.status(_context(), ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID))
    assert status["request_id"] == REQUEST_ID
    assert status["state"] == "admitted"
    assert status["attempts"] == []
    assert status["accounting"] == {
        "estimated": "0",
        "reserved": "0",
        "used": "0",
        "corrected": "0",
        "currency": "USD",
    }
    assert "fingerprint" not in status
    assert "credential" not in status
    point = views.resume_point(
        _context("model.create"), ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    )
    assert point.state.value == "admitted"
    assert point.state_revision == 1


def test_status_orders_failed_first_and_successful_fallback_decisions(
    database_url: str,
) -> None:
    """Return safe ordered attempt decisions without retained request content."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            """INSERT INTO router.provider_model_routes (
                   id, owner_kind, provider_instance_id, canonical_model_id,
                   provider_lookup_id, settings_schema_name, settings_schema_major,
                   settings, current_revision, wire_model
               ) VALUES (%s, 'global', %s, %s, 'fixture-fallback-wire',
                         'route.settings', 1, '{}', %s, 'fixture-fallback-wire')""",
            (
                FALLBACK_ROUTE_ID,
                FIXTURE_INSTANCE_ID,
                FIXTURE_MODEL_ID,
                CONFIGURATION_ID,
            ),
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates (
                   assignment_id, configuration_revision_id, ordinal,
                   provider_model_route_id, attempt_timeout_seconds,
                   attempt_timeout_ms
               ) VALUES (%s, %s, 2, %s, 30, 30000)""",
            (FIXTURE_ASSIGNMENT_ID, CONFIGURATION_ID, FALLBACK_ROUTE_ID),
        )
        connection.execute(
            """INSERT INTO router.active_configurations (
                   scope_kind, service_id, workspace_id, revision_id, revision_number
               ) VALUES ('workspace', %s, %s, %s, 1)""",
            (SERVICE_ID, WORKSPACE_ID, CONFIGURATION_ID),
        )
        connection.execute(
            """INSERT INTO router.route_price_versions (
                   id, provider_model_route_id, version_number, currency, status
               ) VALUES (%s, %s, 1, 'USD', 'current'),
                        (%s, %s, 1, 'USD', 'current')""",
            (
                PRIMARY_PRICE_ID,
                FIXTURE_ROUTE_ID,
                FALLBACK_PRICE_ID,
                FALLBACK_ROUTE_ID,
            ),
        )
        connection.execute(
            """INSERT INTO router.route_price_components (
                   price_version_id, component_kind, unit_name, unit_quantity,
                   unit_price, raw_source_value
               ) VALUES (%s, 'usage', 'input_token', 1, 0.1, '0.1'),
                        (%s, 'usage', 'input_token', 1, 0.1, '0.1')""",
            (PRIMARY_PRICE_ID, FALLBACK_PRICE_ID),
        )
        connection.execute(
            """INSERT INTO router.configuration_price_bindings (
                   configuration_revision_id, provider_model_route_id,
                   price_version_id
               ) VALUES (%s, %s, %s), (%s, %s, %s)""",
            (
                CONFIGURATION_ID,
                FIXTURE_ROUTE_ID,
                PRIMARY_PRICE_ID,
                CONFIGURATION_ID,
                FALLBACK_ROUTE_ID,
                FALLBACK_PRICE_ID,
            ),
        )
        admitted = connection.execute(
            """SELECT admitted_at, configuration_revision_id, assignment_id
               FROM router.logical_requests WHERE row_id = %s""",
            (REQUEST_ROW_ID,),
        ).fetchone()
        assert admitted is not None
        _snapshot_routing_chain(
            connection,
            request_row_id=uuid.UUID(REQUEST_ROW_ID),
            request_id=REQUEST_ID,
            service_id=SERVICE_ID,
            workspace_id=WORKSPACE_ID,
            assignment_revision_id=admitted["configuration_revision_id"],
            assignment_id=admitted["assignment_id"],
            exact_route_id=None,
            admitted_at=admitted["admitted_at"],
        )
        connection.commit()
        PostgresExecutionRepository(database_url).transition(
            _context("model.create"),
            ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID),
            expected_revision=1,
            new_state=ExecutionState.RUNNING,
        )
        evidence = """jsonb_build_object(
            'provider_status', 401, 'retry_after_ms', NULL,
            'detail_code', 'safe_authentication')"""
        connection.execute(
            f"""INSERT INTO router.provider_attempts (
                   id, request_row_id, service_id, workspace_id, attempt_number,
                   provider_model_route_id, route_generation,
                   assignment_revision_id, price_version_id, route_snapshot_id,
                   candidate_ordinal, provider_instance_id,
                   provider_instance_generation, credential_id,
                   credential_generation, connect_timeout_ms,
                   first_byte_timeout_ms, idle_timeout_ms, execution_timeout_ms,
                   logical_deadline, attempt_deadline, state, started_at,
                   finished_at, normalized_error_class, affected_scope,
                   affected_scope_id, retry_decision, safe_provider_code,
                   redacted_evidence, migration_0015_backfilled
               ) SELECT %s, request.row_id, request.service_id,
                        request.workspace_id, 1, snapshot.provider_model_route_id,
                        snapshot.route_generation, snapshot.assignment_revision_id,
                        snapshot.price_version_id, snapshot.id,
                        snapshot.candidate_ordinal, snapshot.provider_instance_id,
                        snapshot.provider_instance_generation,
                        snapshot.credential_id, snapshot.credential_generation,
                        10000, 30000, 30000, 30000,
                        request.admitted_at + interval '15 minutes',
                        request.admitted_at + interval '30 seconds', 'failed',
                        request.admitted_at, request.admitted_at + interval '1 second',
                        'authentication', 'credential', snapshot.credential_id::text,
                        'next_candidate', 'AUTH_401', {evidence}, true
                 FROM router.logical_requests AS request
                 JOIN router.provider_route_execution_snapshots AS snapshot
                   ON snapshot.request_row_id = request.row_id
                  AND snapshot.candidate_ordinal = 1
                 WHERE request.row_id = %s""",  # noqa: S608  # nosec B608 - Fixed SQL fragment.
            (FAILED_ATTEMPT_ID, REQUEST_ROW_ID),
        )
        connection.execute(
            f"""INSERT INTO router.routing_candidate_decisions (
                   decision_id, request_row_id, decision_sequence, attempt_id,
                   attempt_number, candidate_ordinal, route_snapshot_id,
                   attempt_state, normalized_error_class, affected_scope,
                   affected_scope_id, fallback_decision, safe_provider_code,
                   redacted_evidence, occurred_at, migration_0015_backfilled
               ) SELECT %s, attempt.request_row_id, 1, attempt.id,
                        attempt.attempt_number, attempt.candidate_ordinal,
                        attempt.route_snapshot_id, attempt.state::text,
                        attempt.normalized_error_class, attempt.affected_scope,
                        attempt.affected_scope_id, attempt.retry_decision,
                        attempt.safe_provider_code, {evidence},
                        attempt.finished_at, true
                 FROM router.provider_attempts AS attempt WHERE attempt.id = %s""",  # noqa: S608  # nosec B608 - Fixed SQL fragment.
            (FAILED_ATTEMPT_ID, FAILED_ATTEMPT_ID),
        )
        connection.execute(
            """INSERT INTO router.provider_attempts (
                   id, request_row_id, service_id, workspace_id, attempt_number,
                   provider_model_route_id, route_generation,
                   assignment_revision_id, price_version_id, route_snapshot_id,
                   candidate_ordinal, provider_instance_id,
                   provider_instance_generation, credential_id,
                   credential_generation, connect_timeout_ms,
                   first_byte_timeout_ms, idle_timeout_ms, execution_timeout_ms,
                   logical_deadline, attempt_deadline, state, started_at,
                   finished_at, retry_decision, migration_0015_backfilled
               ) SELECT %s, request.row_id, request.service_id,
                        request.workspace_id, 2, snapshot.provider_model_route_id,
                        snapshot.route_generation, snapshot.assignment_revision_id,
                        snapshot.price_version_id, snapshot.id,
                        snapshot.candidate_ordinal, snapshot.provider_instance_id,
                        snapshot.provider_instance_generation,
                        snapshot.credential_id, snapshot.credential_generation,
                        10000, 30000, 30000, 30000,
                        request.admitted_at + interval '15 minutes',
                        request.admitted_at + interval '31 seconds', 'succeeded',
                        request.admitted_at + interval '1 second',
                        request.admitted_at + interval '2 seconds', 'succeeded', true
                 FROM router.logical_requests AS request
                 JOIN router.provider_route_execution_snapshots AS snapshot
                   ON snapshot.request_row_id = request.row_id
                  AND snapshot.candidate_ordinal = 2
                 WHERE request.row_id = %s""",
            (SUCCEEDED_ATTEMPT_ID, REQUEST_ROW_ID),
        )
        connection.execute(
            """INSERT INTO router.routing_candidate_decisions (
                   decision_id, request_row_id, decision_sequence, attempt_id,
                   attempt_number, candidate_ordinal, route_snapshot_id,
                   attempt_state, fallback_decision, occurred_at,
                   migration_0015_backfilled
               ) SELECT %s, attempt.request_row_id, 2, attempt.id,
                        attempt.attempt_number, attempt.candidate_ordinal,
                        attempt.route_snapshot_id, attempt.state::text,
                        attempt.retry_decision, attempt.finished_at, true
                 FROM router.provider_attempts AS attempt WHERE attempt.id = %s""",
            (SUCCEEDED_ATTEMPT_ID, SUCCEEDED_ATTEMPT_ID),
        )
        connection.execute(
            """INSERT INTO router.routing_attempt_usage_reports (
                   attempt_id, usage_components, reported_at
               ) VALUES (%s, '[{"unit":"output_token","quantity":"2"}]',
                         transaction_timestamp())""",
            (SUCCEEDED_ATTEMPT_ID,),
        )

    status = PostgresModelRequestViews(database_url).status(
        _context(), ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    )
    attempts = status["attempts"]
    assert isinstance(attempts, list)
    assert [item["attempt_id"] for item in attempts] == [
        FAILED_ATTEMPT_ID,
        SUCCEEDED_ATTEMPT_ID,
    ]
    assert attempts[0]["decision"] == "next_candidate"
    assert attempts[0]["error"] == {
        "class": "authentication",
        "affected_scope": "credential",
        "message": "The provider attempt did not complete.",
        "safe_provider_code": "AUTH_401",
    }
    assert attempts[1]["decision"] == "succeeded"
    assert attempts[1]["usage"] == [{"unit": "output_token", "quantity": "2"}]
    encoded = str(status)
    assert "private" not in encoded
    assert "credential_id" not in encoded


def test_views_reconstruct_only_completed_retained_text(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)

    target = ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    execution = PostgresExecutionRepository(database_url)
    write = _context("model.create")
    read = _context()
    execution.transition(
        write,
        target,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    execution.append_event(
        write,
        target,
        event_name="output.delta",
        payload={
            "output_index": 0,
            "content_type": "text/plain",
            "delta": "retained ",
        },
    )

    views = PostgresModelRequestViews(database_url)
    assert "result" not in views.status(read, target)

    execution.append_event(
        write,
        target,
        event_name="output.delta",
        payload={
            "output_index": 0,
            "content_type": "text/plain",
            "delta": "result",
        },
    )
    execution.append_event(
        write,
        target,
        event_name="output.completed",
        payload={"output_index": 0, "content_type": "text/plain"},
    )
    execution.transition(
        write,
        target,
        expected_revision=2,
        new_state=ExecutionState.SUCCEEDED,
    )

    status = views.status(read, target)
    assert status["state"] == "succeeded"
    assert status["result"] == {
        "outputs": [{"type": "text", "text": "retained result"}]
    }

"""PostgreSQL accounting and price synchronization tests."""
# ruff: noqa: D103, E501, FBT003, FURB157, PLR0915, PLR2004

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from llmrouter_backend.accounting import (
    AccountingCorrection,
    AccountingError,
    AccountingEvent,
    AccountingSubjectKind,
    AttemptOutcome,
    CorrectionKind,
    PostgresAccountingRepository,
    PriceComponent,
    RawPriceComponent,
    SourceSnapshot,
    SynchronizationStatus,
    UsageComponent,
    UsageDelta,
    UsageUnit,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
)

from .helpers import (
    CONFIGURATION_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_assignment,
    insert_request,
    seed_request_target,
    seed_scope,
)

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
NODE_ID = "0198a080-0000-7000-8000-000000000110"
BUDGET_ID = "0198a080-0000-7000-8000-000000000111"
EVENT_ID = "0198a080-0000-7000-8000-000000000112"
LEGACY_LEDGER_EVENT_ID = "0198a080-0000-7000-8000-000000000136"
CANONICAL_ID = "0198a080-0000-7000-8000-000000000113"
CORRECTION_ONE = "0198a080-0000-7000-8000-000000000114"
CORRECTION_TWO = "0198a080-0000-7000-8000-000000000115"
SERVICE_REQUEST_ROW = "0198a080-0000-7000-8000-000000000116"
SERVICE_REQUEST = "0198a080-0000-7000-8000-000000000117"
SERVICE_EVENT = "0198a080-0000-7000-8000-000000000118"
SERVICE_CANONICAL = "0198a080-0000-7000-8000-000000000119"
SERVICE_BUDGET = "0198a080-0000-7000-8000-000000000120"
GLOBAL_REVISION = "0198a080-0000-7000-8000-000000000121"
ADAPTER_ID = "provider.test"
MODEL_ID = "0198a080-0000-7000-8000-000000000122"
CREDENTIAL_ID = "0198a080-0000-7000-8000-000000000123"
INSTANCE_ID = "0198a080-0000-7000-8000-000000000124"
ROUTE_ID = "0198a080-0000-7000-8000-000000000125"
SOURCE_ID = "0198a080-0000-7000-8000-000000000126"
ASSIGNMENT_ID = "0198a080-0000-7000-8000-000000000135"
BAD_ROUTE_ID = "0198a080-0000-7000-8000-000000000129"
MISSING_ROUTE_ID = "0198a080-0000-7000-8000-000000000130"
MANUAL_ROUTE_ID = "0198a080-0000-7000-8000-000000000131"
SERVICE_ROUTE_ID = "0198a080-0000-7000-8000-000000000137"
SERVICE_SOURCE_ID = "0198a080-0000-7000-8000-000000000138"
SERVICE_REVISION = "0198a080-0000-7000-8000-000000000139"
SERVICE_REQUEST_ASSIGNMENT = "0198a080-0000-7000-8000-000000000140"
SERVICE_GLOBAL_BUDGET = "0198a080-0000-7000-8000-000000000141"


def _record_budget_limit(connection: psycopg.Connection[Any], budget_id: str) -> None:
    row = connection.execute(
        """SELECT hard_limit, warning_threshold, currency::text, revision,
                  reset_period, effective_at
           FROM router.budget_scopes WHERE id = %s""",
        (budget_id,),
    ).fetchone()
    assert row is not None
    operation_id = uuid.uuid4()
    connection.execute(
        """INSERT INTO router.audit_events (
               event_id, audit_class, actor_kind, actor_id, authority_class,
               action, permission_result, safe_details, occurred_at
           ) VALUES (
               %s, 'security', 'system', 'accounting-test', 'system',
               'budget.write', 'permitted',
               '{"resource_type":"budget_limit"}', %s
           )""",
        (operation_id, row[5]),
    )
    connection.execute(
        """INSERT INTO router.budget_limit_operations (
               operation_id, budget_scope_id, actor_id, idempotency_key,
               request_fingerprint, expected_revision, resulting_revision,
               hard_limit, warning_threshold, currency, reset_period,
               audit_event_id, effective_at
           ) VALUES (
               %s, %s, 'accounting-test', %s, %s, 0, %s, %s, %s, %s, %s,
               %s, %s
           )""",
        (
            operation_id,
            budget_id,
            f"accounting-budget-{budget_id}",
            bytes.fromhex("44" * 32),
            row[3],
            row[0],
            row[1],
            row[2],
            row[4],
            operation_id,
            row[5],
        ),
    )


def _system(operation: str) -> RequestContext:
    return RequestContext(
        operation,
        PrincipalKind.SYSTEM,
        "accounting-worker",
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        operation,
        Scope(),
        NOW,
        None,
        True,
    )


def _read(scope: Scope) -> RequestContext:
    return RequestContext(
        "accounting-read",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.ACCOUNTING,
        "accounting.read",
        scope,
        NOW,
        None,
        False,
    )


def _administrator() -> RequestContext:
    return RequestContext(
        "price-sync",
        PrincipalKind.ADMINISTRATOR,
        "issuer:administrator",
        AuthorityClass.GLOBAL_ADMINISTRATOR,
        AuthorityPath.GLOBAL_ADMINISTRATION,
        None,
        "provider_route.manage",
        Scope(),
        NOW,
        NOW,
        True,
    )


def _snapshot(  # noqa: PLR0913
    source_name: str,
    fetched_at: datetime,
    rows: dict[str, tuple[PriceComponent | RawPriceComponent, ...]],
    *,
    source_revision: str | None = None,
    http_validator: str | None = None,
    source_available: bool = True,
) -> SourceSnapshot:
    return SourceSnapshot(
        source_name,
        fetched_at,
        SourceSnapshot.digest(rows, source_available=source_available),
        rows,
        source_revision,
        http_validator,
        source_available,
    )


def _seed_accounting(
    connection: psycopg.Connection[Any],
    payload_sha256: bytes = bytes.fromhex("11" * 32),
) -> None:
    seed_scope(connection)
    insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
    connection.execute(
        """
        INSERT INTO router.budget_scopes (
            id, scope_kind, service_id, workspace_id, currency, hard_limit
        ) VALUES (%s, 'workspace', %s, %s, 'USD', 100)
        """,
        (BUDGET_ID, SERVICE_ID, WORKSPACE_ID),
    )
    _record_budget_limit(connection, BUDGET_ID)
    connection.execute(
        """
        INSERT INTO router.canonical_events (
            event_id, source_node_id, source_sequence, event_class,
            payload_sha256, durable_replay_position, occurred_at
        ) VALUES (%s, %s, 1, 'accounting', %s,
                  'accounting-1', %s)
        """,
        (CANONICAL_ID, NODE_ID, payload_sha256, NOW),
    )
    connection.execute(
        """INSERT INTO router.external_tool_attempt_identities (
               id, request_row_id, service_id, workspace_id
           ) VALUES (%s, %s, %s, %s)""",
        (
            "0198a080-0000-7000-8000-000000000127",
            REQUEST_ROW_ID,
            SERVICE_ID,
            WORKSPACE_ID,
        ),
    )


def test_replay_corrections_and_daily_rebuild_are_exact(database_url: str) -> None:
    event = AccountingEvent(
        EVENT_ID,
        CANONICAL_ID,
        REQUEST_ROW_ID,
        SERVICE_ID,
        WORKSPACE_ID,
        BUDGET_ID,
        AccountingSubjectKind.EXTERNAL_TOOL_ATTEMPT,
        "0198a080-0000-7000-8000-000000000127",
        AttemptOutcome.FAILED,
        "USD",
        (UsageComponent(UsageUnit.INPUT_TOKEN, Decimal("10")),),
        NOW,
        reported_amount=Decimal("1.2"),
        budget_ledger_event_id=LEGACY_LEDGER_EVENT_ID,
    )
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed_accounting(connection, event.canonical_payload_sha256())
        connection.execute(
            """INSERT INTO router.accounting_events (
                   event_id, request_row_id, budget_scope_id, currency,
                   event_kind, quantity, amount, occurred_at
               ) VALUES (%s, %s, %s, 'USD', 'usage', 10, 1.2, %s)""",
            (LEGACY_LEDGER_EVENT_ID, REQUEST_ROW_ID, BUDGET_ID, NOW),
        )
    execution = PostgresExecutionRepository(database_url)
    execution_context = RequestContext(
        "late-accounting-execution",
        PrincipalKind.SYSTEM,
        "accounting-worker",
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        "model.create",
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        True,
    )
    target = ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    execution.transition(
        execution_context,
        target,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    execution.transition(
        execution_context,
        target,
        expected_revision=2,
        new_state=ExecutionState.SUCCEEDED,
    )
    with psycopg.connect(database_url) as connection:
        lifecycle_before = connection.execute(
            """SELECT state, state_revision, last_transition_at, terminal_at,
                      partial_output, committed_effect
               FROM router.logical_requests WHERE row_id = %s""",
            (REQUEST_ROW_ID,),
        ).fetchone()
    repository = PostgresAccountingRepository(database_url)
    assert not repository.ingest(_system("accounting.ingest"), event)
    assert repository.ingest(_system("accounting.ingest"), event)
    first = AccountingCorrection(
        CORRECTION_ONE,
        EVENT_ID,
        CorrectionKind.PROVIDER_USAGE,
        "USD",
        Decimal("-0.2"),
        (UsageDelta(UsageUnit.INPUT_TOKEN, Decimal("-2")),),
        "provider-report",
        "The provider corrected the usage.",
        NOW + timedelta(days=1),
    )
    second = AccountingCorrection(
        CORRECTION_TWO,
        EVENT_ID,
        CorrectionKind.INVOICE,
        "USD",
        Decimal("0.1"),
        (UsageDelta(UsageUnit.INPUT_TOKEN, Decimal("1")),),
        "invoice",
        "The invoice corrected the usage.",
        NOW + timedelta(days=1),
    )
    assert not repository.append_correction(_system("accounting.correct"), first)
    assert repository.append_correction(_system("accounting.correct"), first)
    assert not repository.append_correction(_system("accounting.correct"), second)
    conflicting = AccountingCorrection(
        CORRECTION_ONE,
        EVENT_ID,
        CorrectionKind.PROVIDER_USAGE,
        "USD",
        Decimal("-0.2"),
        (UsageDelta(UsageUnit.INPUT_TOKEN, Decimal(-3)),),
        "provider-report",
        "The provider corrected the usage.",
        NOW + timedelta(days=1),
    )
    with pytest.raises(AccountingError):
        repository.append_correction(_system("accounting.correct"), conflicting)
    underflow = AccountingCorrection(
        "0198a080-0000-7000-8000-000000000128",
        EVENT_ID,
        CorrectionKind.PROVIDER_USAGE,
        "USD",
        Decimal(0),
        (UsageDelta(UsageUnit.INPUT_TOKEN, Decimal(-10)),),
        "provider-report",
        "The provider reported an invalid negative total.",
        NOW + timedelta(days=1),
    )
    with pytest.raises(AccountingError):
        repository.append_correction(_system("accounting.correct"), underflow)
    summary = repository.summary(
        _read(Scope(SERVICE_ID, WORKSPACE_ID)),
        Scope(SERVICE_ID, WORKSPACE_ID),
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(days=2),
    )
    assert summary.cost == Decimal("1.2")
    assert summary.corrections == Decimal("-0.1")
    assert summary.usage[0].quantity == Decimal("9")
    assert repository.rebuild_daily_aggregates(_system("accounting.aggregate")) == 2
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """SELECT accounting_day, logical_requests, attempts, cost,
                      corrections, usage
               FROM router.daily_accounting_aggregates ORDER BY accounting_day"""
        ).fetchall()
    assert rows == [
        (NOW.date(), 1, 1, Decimal("1.2"), Decimal("0"), {"input_token": 10}),
        (
            (NOW + timedelta(days=1)).date(),
            0,
            0,
            Decimal("0"),
            Decimal("-0.1"),
            {"input_token": -1},
        ),
    ]
    with psycopg.connect(database_url) as connection:
        lifecycle_after = connection.execute(
            """SELECT state, state_revision, last_transition_at, terminal_at,
                      partial_output, committed_effect
               FROM router.logical_requests WHERE row_id = %s""",
            (REQUEST_ROW_ID,),
        ).fetchone()
    assert lifecycle_after == lifecycle_before


def test_service_scope_daily_aggregate_accepts_null_workspace(
    database_url: str,
) -> None:
    event = AccountingEvent(
        SERVICE_EVENT,
        SERVICE_CANONICAL,
        SERVICE_REQUEST_ROW,
        SERVICE_ID,
        None,
        SERVICE_BUDGET,
        AccountingSubjectKind.LOGICAL_REQUEST,
        SERVICE_REQUEST_ROW,
        AttemptOutcome.SUCCEEDED,
        "USD",
        (UsageComponent(UsageUnit.REQUEST, Decimal(1)),),
        NOW,
        reported_amount=Decimal("0.5"),
    )
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed_accounting(connection)
        connection.commit()
        connection.execute(
            """INSERT INTO router.configuration_revisions (
                   id, scope_kind, service_id, revision_number, content,
                   content_sha256, created_by_kind, created_by_id
               ) VALUES (%s, 'service', %s, 1, '{}',
                         decode(repeat('12', 32), 'hex'), 'system', 'test')""",
            (GLOBAL_REVISION, SERVICE_ID),
        )
        seed_request_target(connection)
        insert_assignment(connection, SERVICE_REQUEST_ASSIGNMENT, GLOBAL_REVISION)
        connection.execute(
            """INSERT INTO router.logical_requests (
                   row_id, request_id, request_kind, service_id,
                   assignment_id, configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, data_profile, capture_enabled
               ) VALUES (%s, %s, 'model', %s, %s, %s, 1,
                         decode(repeat('13', 32), 'hex'), 'service-data', true)""",
            (
                SERVICE_REQUEST_ROW,
                SERVICE_REQUEST,
                SERVICE_ID,
                SERVICE_REQUEST_ASSIGNMENT,
                GLOBAL_REVISION,
            ),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
               id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (SERVICE_GLOBAL_BUDGET,),
        )
        _record_budget_limit(connection, SERVICE_GLOBAL_BUDGET)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 100)""",
            (SERVICE_BUDGET, SERVICE_ID, SERVICE_GLOBAL_BUDGET),
        )
        _record_budget_limit(connection, SERVICE_BUDGET)
        connection.execute(
            """UPDATE router.budget_scopes
               SET parent_budget_scope_id = %s WHERE id = %s""",
            (SERVICE_BUDGET, BUDGET_ID),
        )
        connection.execute(
            """INSERT INTO router.canonical_events (
                   event_id, source_node_id, source_sequence, event_class,
                   payload_sha256, durable_replay_position, occurred_at
               ) VALUES (%s, %s, 2, 'accounting',
                         %s, 'accounting-2', %s)""",
            (SERVICE_CANONICAL, NODE_ID, event.canonical_payload_sha256(), NOW),
        )
    repository = PostgresAccountingRepository(database_url)
    repository.ingest(
        _system("accounting.ingest"),
        event,
    )
    repository.rebuild_daily_aggregates(_system("accounting.aggregate"))
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """SELECT workspace_id, cost FROM router.daily_accounting_aggregates
               WHERE workspace_id IS NULL"""
        ).fetchone()
    assert row == (None, Decimal("0.5"))


def test_accounting_fact_rejects_audit_canonical_event(database_url: str) -> None:
    audit_event = "0198a080-0000-7000-8000-000000000132"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed_accounting(connection)
        connection.execute(
            """INSERT INTO router.canonical_events (
                   event_id, source_node_id, source_sequence, event_class,
                   payload_sha256, durable_replay_position, occurred_at
               ) VALUES (%s, %s, 3, 'audit',
                         decode(repeat('31', 32), 'hex'), 'audit-3', %s)""",
            (audit_event, NODE_ID, NOW),
        )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.accounting_facts (
                           event_id, canonical_event_id, request_row_id,
                           service_id, workspace_id, budget_scope_id,
                           subject_kind, subject_id, outcome, currency,
                           amount, occurred_at, canonical_payload_sha256
                       ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s,
                                 'logical_request', %s, 'succeeded', 'USD', 0, %s,
                                 decode(repeat('31', 32), 'hex'))""",
                (
                    audit_event,
                    REQUEST_ROW_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    BUDGET_ID,
                    REQUEST_ROW_ID,
                    NOW,
                ),
            )


def test_price_sync_preserves_active_configuration_and_last_good_price(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        connection.execute(
            """
            INSERT INTO router.configuration_revisions (
                id, scope_kind, revision_number, content, content_sha256,
                created_by_kind, created_by_id
            ) VALUES (%s, 'global', 1, '{"sentinel":{"kept":true}}',
                      decode(repeat('21', 32), 'hex'), 'system', 'test')
            """,
            (GLOBAL_REVISION,),
        )
        connection.execute(
            """
            INSERT INTO router.active_configurations (
                scope_kind, revision_id, revision_number
            ) VALUES ('global', %s, 1)
            """,
            (GLOBAL_REVISION,),
        )
        connection.execute(
            """
            INSERT INTO router.provider_adapter_types (
                id, settings_schema_name, settings_schema_major, capabilities
            ) VALUES (%s, 'provider.settings', 1, '{}')
            """,
            (ADAPTER_ID,),
        )
        connection.execute(
            """
            INSERT INTO router.canonical_models (id, stable_name, capabilities)
            VALUES (%s, 'model-test', '{}')
            """,
            (MODEL_ID,),
        )
        connection.execute(
            """
            INSERT INTO router.encrypted_credentials (
                id, owner_kind, credential_kind, ciphertext, encrypted_data_key,
                wrapping_key_id, safe_fingerprint, current_revision,
                last_changed_at
            ) VALUES (%s, 'global', %s, %s, %s, 'wrap', 'safe', %s, %s)
            """,
            (CREDENTIAL_ID, ADAPTER_ID, bytes(32), bytes(32), CREDENTIAL_ID, NOW),
        )
        connection.execute(
            """
            INSERT INTO router.provider_instances (
                id, owner_kind, adapter_type_id, credential_id, stable_name,
                endpoint_origin, settings_schema_name, settings_schema_major, settings
            ) VALUES (%s, 'global', %s, %s, 'instance',
                      'https://provider.example', 'provider.settings', 1, '{}')
            """,
            (INSTANCE_ID, ADAPTER_ID, CREDENTIAL_ID),
        )
        connection.execute(
            """
            INSERT INTO router.provider_model_routes (
                id, owner_kind, provider_instance_id, canonical_model_id,
                provider_lookup_id, settings_schema_name, settings_schema_major, settings
            ) VALUES (%s, 'global', %s, %s, 'wire-model', 'route.settings', 1, '{}')
            """,
            (ROUTE_ID, INSTANCE_ID, MODEL_ID),
        )
        connection.execute(
            """
            INSERT INTO router.route_price_sources (
                id, provider_model_route_id, authority_kind, source_name,
                lookup_identifier
            ) VALUES (%s, %s, 'synchronized', 'catalog-test', 'wire-model')
            """,
            (SOURCE_ID, ROUTE_ID),
        )
        connection.execute(
            """INSERT INTO router.assignment_definitions (
                   id, configuration_revision_id, stable_name
               ) VALUES (%s, %s, 'chat')""",
            (ASSIGNMENT_ID, GLOBAL_REVISION),
        )
        connection.execute(
            """INSERT INTO router.assignment_candidates (
                   assignment_id, configuration_revision_id, ordinal,
                   provider_model_route_id, attempt_timeout_seconds,
                   attempt_timeout_ms, candidate_policy
               ) VALUES (%s, %s, 1, %s, 30, 30000, '{}')""",
            (ASSIGNMENT_ID, GLOBAL_REVISION, ROUTE_ID),
        )
    repository = PostgresAccountingRepository(database_url)
    snapshot = _snapshot(
        "catalog-test",
        NOW,
        {
            "wire-model": (
                PriceComponent(
                    UsageUnit.INPUT_TOKEN,
                    Decimal("0.002"),
                    "USD",
                    "0.002",
                    Decimal("1000"),
                ),
            )
        },
        source_revision="catalog-1",
        http_validator='"etag-1"',
    )
    result = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=snapshot,
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW,
    )
    assert result.rows[0].status is SynchronizationStatus.UPDATED
    assert result.state == "completed"
    assert result.source_snapshot is not None
    assert result.source_snapshot.content_sha256 == snapshot.content_sha256
    assert result.resulting_configuration_revisions == (
        result.resulting_configuration_revision,
    )
    recovered = repository.get_synchronization(_administrator(), result.operation_id)
    assert recovered.resulting_configuration_revision == (
        result.resulting_configuration_revision
    )
    with psycopg.connect(database_url) as connection:
        active = connection.execute(
            """SELECT active.revision_number, revision.content,
                          (SELECT count(*) FROM router.route_price_versions),
                          (SELECT count(*)
                           FROM router.configuration_distribution_states
                           WHERE revision_id = active.revision_id),
                          (SELECT count(*)
                           FROM router.configuration_audit_bindings
                           WHERE revision_id = active.revision_id),
                          (SELECT state FROM router.price_publication_outbox
                           WHERE resulting_configuration_revision_id =
                                 active.revision_id),
                          (SELECT count(*)
                           FROM router.assignment_candidates
                           WHERE configuration_revision_id = active.revision_id),
                          (SELECT count(*)
                           FROM router.configuration_price_bindings
                           WHERE configuration_revision_id = active.revision_id),
                          (SELECT count(*) FROM router.audit_events
                           WHERE event_id = %s)
               FROM router.active_configurations AS active
               JOIN router.configuration_revisions AS revision
                 ON revision.id = active.revision_id
               WHERE active.scope_kind = 'global'""",
            (result.operation_id,),
        ).fetchone()
    assert active == (
        2,
        {"sentinel": {"kept": True}},
        1,
        1,
        1,
        "published",
        1,
        1,
        1,
    )
    recovery_snapshot = _snapshot(
        "catalog-test",
        NOW + timedelta(minutes=1),
        {
            "wire-model": (
                PriceComponent(
                    UsageUnit.INPUT_TOKEN,
                    Decimal("0.0025"),
                    "USD",
                    "0.0025",
                    Decimal("1000"),
                ),
            )
        },
        source_revision="catalog-recovery",
    )
    original_publish = repository.publish_all_pending
    interruption = "The publication worker stopped."

    def interrupted_publish(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise AccountingError(interruption)

    monkeypatch.setattr(repository, "publish_all_pending", interrupted_publish)
    with pytest.raises(AccountingError, match="publication worker stopped"):
        repository.synchronize(
            _administrator(),
            service_id=None,
            snapshot=recovery_snapshot,
            route_ids=(ROUTE_ID,),
            dry_run=False,
            now=NOW + timedelta(minutes=1),
        )
    monkeypatch.setattr(repository, "publish_all_pending", original_publish)
    with psycopg.connect(database_url) as connection:
        interrupted_row = connection.execute(
            """SELECT run.id::text
               FROM router.price_synchronization_runs AS run
               JOIN router.price_source_snapshots AS snapshot
                 ON snapshot.id = run.source_snapshot_id
               WHERE snapshot.content_sha256 = decode(%s, 'hex')""",
            (recovery_snapshot.content_sha256,),
        ).fetchone()
    assert interrupted_row is not None
    interrupted_operation = interrupted_row[0]
    recovered_operations = repository.publish_pending_operations(
        _system("price.publish"),
        now=NOW + timedelta(minutes=2),
    )
    assert recovered_operations == (interrupted_operation,)
    recovered_revisions = repository.get_synchronization(
        _administrator(), interrupted_operation
    ).resulting_configuration_revisions
    assert len(recovered_revisions) == 1
    assert (
        repository.get_synchronization(
            _administrator(), interrupted_operation
        ).resulting_configuration_revisions
        == recovered_revisions
    )
    preview = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=snapshot,
        route_ids=(ROUTE_ID,),
        dry_run=True,
        now=NOW,
        idempotency_key="price-preview-key-0001",
    )
    replay = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=snapshot,
        route_ids=(ROUTE_ID,),
        dry_run=True,
        now=NOW,
        idempotency_key="price-preview-key-0001",
    )
    assert replay.operation_id == preview.operation_id
    assert replay.state == "previewed"
    assert replay.source_snapshot is not None
    assert replay.source_snapshot.http_validator == '"etag-1"'
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT state, source_snapshot_id IS NOT NULL
               FROM router.price_synchronization_runs WHERE id = %s""",
            (preview.operation_id,),
        ).fetchone() == ("previewed", True)
    missing = _snapshot("catalog-test", NOW, {})
    result = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=missing,
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(days=1),
    )
    assert result.rows[0].status is SynchronizationStatus.MISSING
    with psycopg.connect(database_url) as connection:
        version_count = connection.execute(
            "SELECT count(*) FROM router.route_price_versions"
        ).fetchone()
        missing_state = connection.execute(
            """SELECT synchronization_state
               FROM router.route_price_synchronization_states
               WHERE provider_model_route_id = %s""",
            (ROUTE_ID,),
        ).fetchone()
    assert version_count == (2,)
    assert missing_state == ("stale",)
    with psycopg.connect(database_url) as connection:
        for route_id, lookup, authority in (
            (BAD_ROUTE_ID, "bad-model", "synchronized"),
            (MISSING_ROUTE_ID, "missing-model", "synchronized"),
            (MANUAL_ROUTE_ID, "manual-model", "manual"),
        ):
            connection.execute(
                """INSERT INTO router.provider_model_routes (
                       id, owner_kind, provider_instance_id, canonical_model_id,
                       provider_lookup_id, settings_schema_name,
                       settings_schema_major, settings
                   ) VALUES (%s, 'global', %s, %s, %s,
                             'route.settings', 1, '{}')""",
                (route_id, INSTANCE_ID, MODEL_ID, lookup),
            )
            connection.execute(
                """INSERT INTO router.route_price_sources (
                       id, provider_model_route_id, authority_kind,
                       source_name, lookup_identifier
                   ) VALUES (gen_random_uuid(), %s, %s, %s, %s)""",
                (
                    route_id,
                    authority,
                    None if authority == "manual" else "catalog-test",
                    None if authority == "manual" else lookup,
                ),
            )
    mixed = _snapshot(
        "catalog-test",
        NOW + timedelta(days=2),
        {
            "wire-model": (
                RawPriceComponent("input_token", "0.003", "USD", "0.003", "1000"),
            ),
            "bad-model": (RawPriceComponent("unsupported", "bad", "USD", "bad"),),
        },
        http_validator='"etag-mixed"',
    )
    mixed_result = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=mixed,
        route_ids=(ROUTE_ID, BAD_ROUTE_ID, MISSING_ROUTE_ID, MANUAL_ROUTE_ID),
        dry_run=False,
        now=NOW + timedelta(days=2),
    )
    assert {row.lookup_identifier: row.status for row in mixed_result.rows} == {
        "wire-model": SynchronizationStatus.UPDATED,
        "bad-model": SynchronizationStatus.FAILED,
        "missing-model": SynchronizationStatus.MISSING,
        "manual-model": SynchronizationStatus.SKIPPED,
    }
    unavailable = _snapshot(
        "catalog-test",
        NOW + timedelta(days=3),
        {},
        http_validator='"etag-2"',
        source_available=False,
    )
    unavailable_result = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=unavailable,
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(days=3),
    )
    assert unavailable_result.rows[0].error_class == "source_unavailable"
    with psycopg.connect(database_url) as connection:
        evidence = connection.execute(
            """SELECT count(*), count(DISTINCT fetched_at),
                      count(DISTINCT http_validator)
               FROM router.price_source_snapshots
               WHERE content_sha256 = decode(%s, 'hex')""",
            (unavailable.content_sha256,),
        ).fetchone()
        state = connection.execute(
            """SELECT synchronization_state
               FROM router.route_price_synchronization_states
               WHERE provider_model_route_id = %s""",
            (ROUTE_ID,),
        ).fetchone()
        row_states: dict[str, str] = dict(
            connection.execute(
                """SELECT route.provider_lookup_id, state.synchronization_state
                   FROM router.route_price_synchronization_states AS state
                   JOIN router.provider_model_routes AS route
                     ON route.id = state.provider_model_route_id
                   WHERE route.id = ANY(%s::uuid[])""",
                ([BAD_ROUTE_ID, MISSING_ROUTE_ID, MANUAL_ROUTE_ID],),
            ).fetchall()
        )
    assert evidence == (1, 1, 1)
    assert state == ("stale",)
    assert row_states == {
        "bad-model": "failed",
        "missing-model": "missing",
        "manual-model": "manual",
    }
    attempt_id = "0198a080-0000-7000-8000-000000000133"
    canonical_id = "0198a080-0000-7000-8000-000000000134"
    with psycopg.connect(database_url) as connection:
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, 'USD', 100)""",
            (BUDGET_ID, SERVICE_ID, WORKSPACE_ID),
        )
        _record_budget_limit(connection, BUDGET_ID)
        price_versions = connection.execute(
            """SELECT id::text FROM router.route_price_versions
               WHERE provider_model_route_id = %s ORDER BY version_number""",
            (ROUTE_ID,),
        ).fetchall()
        connection.commit()
        PostgresExecutionRepository(database_url).transition(
            RequestContext(
                "accounting-execution",
                PrincipalKind.SYSTEM,
                "accounting-worker",
                AuthorityClass.SYSTEM,
                AuthorityPath.MACHINE,
                None,
                "model.create",
                Scope(SERVICE_ID, WORKSPACE_ID),
                NOW,
                None,
                True,
            ),
            ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID),
            expected_revision=1,
            new_state=ExecutionState.RUNNING,
        )
        connection.execute(
            """INSERT INTO router.provider_attempts (
                   id, request_row_id, service_id, workspace_id, attempt_number,
                   provider_model_route_id, route_generation,
                   assignment_revision_id, price_version_id, state,
                   started_at, finished_at
               ) VALUES (%s, %s, %s, %s, 1, %s, 1, %s, %s,
                         'failed', %s, %s)""",
            (
                attempt_id,
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                ROUTE_ID,
                CONFIGURATION_ID,
                price_versions[-1][0],
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """INSERT INTO router.canonical_events (
                   event_id, source_node_id, source_sequence, event_class,
                   payload_sha256, durable_replay_position, occurred_at
               ) VALUES (%s, %s, 4, 'accounting',
                         decode(repeat('41', 32), 'hex'), 'accounting-4', %s)""",
            (canonical_id, NODE_ID, NOW),
        )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.accounting_facts (
                           event_id, canonical_event_id, request_row_id,
                           service_id, workspace_id, budget_scope_id,
                           subject_kind, subject_id, outcome, currency,
                           price_version_id, amount, occurred_at,
                           canonical_payload_sha256
                       ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s,
                                 'provider_attempt', %s, 'failed', 'USD', %s, 0, %s,
                                 decode(repeat('41', 32), 'hex'))""",
                (
                    canonical_id,
                    REQUEST_ROW_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    BUDGET_ID,
                    attempt_id,
                    price_versions[0][0],
                    NOW,
                ),
            )

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.provider_model_routes (
                   id, owner_kind, owner_service_id, provider_instance_id,
                   canonical_model_id, provider_lookup_id,
                   settings_schema_name, settings_schema_major, settings
               ) VALUES (%s, 'service', %s, %s, %s, 'wire-service',
                         'route.settings', 1, '{}')""",
            (SERVICE_ROUTE_ID, SERVICE_ID, INSTANCE_ID, MODEL_ID),
        )
        connection.execute(
            """INSERT INTO router.route_price_sources (
                   id, provider_model_route_id, authority_kind, source_name,
                   lookup_identifier
               ) VALUES (%s, %s, 'synchronized', 'catalog-test', 'wire-service')""",
            (SERVICE_SOURCE_ID, SERVICE_ROUTE_ID),
        )
        connection.execute(
            """INSERT INTO router.configuration_revisions (
                   id, scope_kind, service_id, revision_number, content,
                   content_sha256, created_by_kind, created_by_id
               ) VALUES (%s, 'service', %s, 1,
                         jsonb_build_object(
                             'provider_model_routes',
                             jsonb_build_array(jsonb_build_object(
                                 'provider_model_route_id', %s::text
                             ))
                         ), decode(repeat('51', 32), 'hex'), 'system', 'test')""",
            (SERVICE_REVISION, SERVICE_ID, SERVICE_ROUTE_ID),
        )
        connection.execute(
            """INSERT INTO router.active_configurations (
                   scope_kind, service_id, revision_id, revision_number
               ) VALUES ('service', %s, %s, 1)""",
            (SERVICE_ID, SERVICE_REVISION),
        )
    multi_snapshot = _snapshot(
        "catalog-test",
        NOW + timedelta(days=4),
        {
            "wire-model": (
                PriceComponent(
                    UsageUnit.INPUT_TOKEN,
                    Decimal("0.004"),
                    "USD",
                    "0.004",
                    Decimal("1000"),
                ),
            ),
            "wire-service": (
                PriceComponent(
                    UsageUnit.INPUT_TOKEN,
                    Decimal("0.005"),
                    "USD",
                    "0.005",
                    Decimal("1000"),
                ),
            ),
        },
        source_revision="catalog-multi-owner",
    )
    multi_result = repository.synchronize(
        _administrator(),
        service_id=None,
        snapshot=multi_snapshot,
        route_ids=(ROUTE_ID, SERVICE_ROUTE_ID),
        dry_run=False,
        now=NOW + timedelta(days=4),
    )
    assert multi_result.resulting_configuration_revision is None
    assert len(multi_result.resulting_configuration_revisions) == 2
    with psycopg.connect(database_url) as connection:
        publication_scopes = connection.execute(
            """SELECT revision.scope_kind, revision.service_id::text,
                      binding.provider_model_route_id::text
               FROM router.price_synchronization_publications AS publication
               JOIN router.configuration_revisions AS revision
                 ON revision.id = publication.configuration_revision_id
               JOIN router.configuration_price_bindings AS binding
                 ON binding.configuration_revision_id = revision.id
               WHERE publication.synchronization_run_id = %s
               ORDER BY revision.scope_kind""",
            (multi_result.operation_id,),
        ).fetchall()
        publication_audit_row = connection.execute(
            """SELECT count(*) FROM router.audit_events
               WHERE action = 'price.publish'
                 AND safe_details ->> 'synchronization_run_id' = %s""",
            (multi_result.operation_id,),
        ).fetchone()
    assert publication_audit_row is not None
    publication_audits = publication_audit_row[0]
    assert publication_scopes == [
        ("global", None, ROUTE_ID),
        ("service", SERVICE_ID, SERVICE_ROUTE_ID),
    ]
    assert publication_audits == 2
    due = repository.due_synchronizations(
        _system("price.schedule"),
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    due_groups = {(owner, source): routes for owner, source, routes in due}
    assert ROUTE_ID in due_groups[(None, "catalog-test")]
    assert SERVICE_ROUTE_ID in due_groups[(SERVICE_ID, "catalog-test")]
    with psycopg.connect(database_url) as connection:
        stale_states: dict[str, str] = dict(
            connection.execute(
                """SELECT provider_model_route_id::text, synchronization_state
                   FROM router.route_price_synchronization_states
                   WHERE provider_model_route_id = ANY(%s::uuid[])""",
                ([ROUTE_ID, SERVICE_ROUTE_ID],),
            ).fetchall()
        )
    assert stale_states == {ROUTE_ID: "stale", SERVICE_ROUTE_ID: "stale"}

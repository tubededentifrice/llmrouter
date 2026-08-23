"""PostgreSQL hierarchical budget policy tests."""
# ruff: noqa: D103, E501, FBT003, PLR0913, PLR2004

from __future__ import annotations

import concurrent.futures
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    BudgetCandidateKind,
    BudgetError,
    BudgetErrorCode,
    BudgetScopeKind,
    BudgetTarget,
    EnforcementState,
    PostgresBudgetRepository,
    ReservationResult,
    ReservationState,
    ResetPeriod,
)
from llmrouter_backend.database import migrate

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ASSIGNMENT_ID,
    FIXTURE_ROUTE_ID,
    OTHER_SERVICE_ID,
    OTHER_WORKSPACE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_request_target,
    seed_scope,
)

NOW = datetime(2026, 9, 15, 10, tzinfo=UTC)
GLOBAL_BUDGET = "0198a080-0000-7000-8000-000000000201"
SERVICE_BUDGET = "0198a080-0000-7000-8000-000000000202"
WORKSPACE_BUDGET = "0198a080-0000-7000-8000-000000000203"
ASSIGNMENT_BUDGET = "0198a080-0000-7000-8000-000000000204"
CANDIDATE_ONE = "0198a080-0000-7000-8000-000000000205"
CANDIDATE_TWO = "0198a080-0000-7000-8000-000000000206"
SECOND_ROW = "0198a080-0000-7000-8000-000000000207"
SECOND_REQUEST = "0198a080-0000-7000-8000-000000000208"
ACCOUNTING_EVENT = "0198a080-0000-7000-8000-000000000213"
CANONICAL_EVENT = "0198a080-0000-7000-8000-000000000214"
NODE_ID = "0198a080-0000-7000-8000-000000000215"
ACCOUNTING_CORRECTION = "0198a080-0000-7000-8000-000000000216"
OTHER_SERVICE_BUDGET = "0198a080-0000-7000-8000-000000000230"


def _system(operation: str) -> RequestContext:
    return RequestContext(
        operation,
        PrincipalKind.SYSTEM,
        "budget-worker",
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        operation,
        Scope(),
        NOW,
        None,
        True,
    )


def _ceiling(operation: str, *, mutation: bool) -> RequestContext:
    return RequestContext(
        operation,
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.BUDGET_AUTHORITY,
        operation,
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        mutation,
    )


def _budget(operation: str, *, mutation: bool = False) -> RequestContext:
    return RequestContext(
        operation,
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.CONFIGURATION,
        operation,
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        mutation,
    )


def _service_budget_write() -> RequestContext:
    return RequestContext(
        "budget-write",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.CONFIGURATION,
        "budget.write",
        Scope(SERVICE_ID),
        NOW,
        None,
        mutation=True,
    )


def _administrator_budget_write(target: BudgetTarget) -> RequestContext:
    return RequestContext(
        "budget-write",
        PrincipalKind.ADMINISTRATOR,
        "administrator-one",
        AuthorityClass.GLOBAL_ADMINISTRATOR,
        AuthorityPath.GLOBAL_ADMINISTRATION,
        None,
        "budget.write",
        Scope(target.service_id, target.workspace_id),
        NOW,
        None,
        mutation=True,
    )


def _seed(connection: psycopg.Connection[object]) -> None:
    seed_scope(connection)
    insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)


def _insert_hierarchy(
    connection: psycopg.Connection[object],
    *,
    workspace_limit: Decimal = Decimal(60),
    reset_period: str = "none",
) -> None:
    connection.execute(
        """INSERT INTO router.budget_scopes (
               id, scope_kind, currency, hard_limit, warning_threshold,
               reset_period
           ) VALUES (%s, 'global', 'USD', 100, 90, %s)""",
        (GLOBAL_BUDGET, reset_period),
    )
    connection.execute(
        """INSERT INTO router.budget_scopes (
               id, scope_kind, service_id, parent_budget_scope_id, currency,
               hard_limit, warning_threshold, reset_period
           ) VALUES (%s, 'service', %s, %s, 'USD', 80, 70, %s)""",
        (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET, reset_period),
    )
    connection.execute(
        """INSERT INTO router.budget_scopes (
               id, scope_kind, service_id, workspace_id,
               parent_budget_scope_id, currency, hard_limit,
               warning_threshold, reset_period
           ) VALUES (%s, 'workspace', %s, %s, %s, 'USD', %s, 40, %s)""",
        (
            WORKSPACE_BUDGET,
            SERVICE_ID,
            WORKSPACE_ID,
            SERVICE_BUDGET,
            workspace_limit,
            reset_period,
        ),
    )
    connection.execute(
        """INSERT INTO router.budget_scopes (
               id, scope_kind, service_id, workspace_id, assignment_id,
               parent_budget_scope_id, currency, hard_limit, warning_threshold,
               reset_period
           ) VALUES (
               %s, 'assignment', %s, %s, %s, %s, 'USD', 50, 45, %s
           )""",
        (
            ASSIGNMENT_BUDGET,
            SERVICE_ID,
            WORKSPACE_ID,
            FIXTURE_ASSIGNMENT_ID,
            WORKSPACE_BUDGET,
            reset_period,
        ),
    )
    _record_direct_budget_operations(connection)


def _record_direct_budget_operations(
    connection: psycopg.Connection[Any],
) -> None:
    rows = connection.execute(
        """SELECT budget.id::text, budget.hard_limit,
                  budget.warning_threshold, budget.currency::text,
                  budget.revision, budget.reset_period, budget.effective_at
           FROM router.budget_scopes AS budget
           WHERE budget.scope_kind <> 'host_ceiling'
             AND NOT EXISTS (
                 SELECT 1 FROM router.budget_limit_operations AS operation
                 WHERE operation.budget_scope_id = budget.id
                   AND operation.resulting_revision = budget.revision
             )"""
    ).fetchall()
    for (
        scope_id,
        hard_limit,
        warning_threshold,
        currency,
        revision,
        reset_period,
        effective_at,
    ) in rows:
        operation_id = uuid.uuid4()
        connection.execute(
            """INSERT INTO router.audit_events (
                   event_id, audit_class, actor_kind, actor_id, authority_class,
                   action, permission_result, safe_details, occurred_at
               ) VALUES (
                   %s, 'security', 'system', 'budget-test', 'system',
                   'budget.write', 'permitted',
                   '{"resource_type":"budget_limit"}', %s
               )""",
            (operation_id, effective_at),
        )
        connection.execute(
            """INSERT INTO router.budget_limit_operations (
                   operation_id, budget_scope_id, actor_id, idempotency_key,
                   request_fingerprint, expected_revision, resulting_revision,
                   hard_limit, warning_threshold, currency, reset_period,
                   audit_event_id, effective_at
               ) VALUES (
                   %s, %s, 'budget-test', %s, %s, 0, %s, %s, %s, %s, %s,
                   %s, %s
               )""",
            (
                operation_id,
                scope_id,
                f"test-budget-{scope_id}",
                bytes.fromhex("33" * 32),
                revision,
                hard_limit,
                warning_threshold,
                currency,
                reset_period,
                operation_id,
                effective_at,
            ),
        )


def _insert_accounting_fact(
    connection: psycopg.Connection[object],
    *,
    amount: Decimal,
    occurred_at: datetime,
    event_id: str = ACCOUNTING_EVENT,
    canonical_id: str = CANONICAL_EVENT,
    candidate_id: str = CANDIDATE_ONE,
    budget_scope_id: str = WORKSPACE_BUDGET,
    assignment_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO router.canonical_events (
               event_id, source_node_id, source_sequence, event_class,
               payload_sha256, durable_replay_position, occurred_at
           ) VALUES (%s, %s, 1, 'accounting', %s, 'budget-test', %s)""",
        (canonical_id, NODE_ID, bytes.fromhex("11" * 32), occurred_at),
    )
    connection.execute(
        """INSERT INTO router.external_tool_attempt_identities (
               id, request_row_id, service_id, workspace_id
           ) VALUES (%s, %s, %s, %s)""",
        (candidate_id, REQUEST_ROW_ID, SERVICE_ID, WORKSPACE_ID),
    )
    connection.execute(
        """INSERT INTO router.accounting_facts (
               event_id, canonical_event_id, request_row_id, service_id,
               workspace_id, budget_scope_id, subject_kind, subject_id,
               outcome, currency, amount, occurred_at, canonical_payload_sha256
               , assignment_id
           ) VALUES (%s, %s, %s, %s, %s, %s, 'external_tool_attempt', %s,
                     'succeeded', 'USD', %s, %s, %s, %s)""",
        (
            event_id,
            canonical_id,
            REQUEST_ROW_ID,
            SERVICE_ID,
            WORKSPACE_ID,
            budget_scope_id,
            candidate_id,
            amount,
            occurred_at,
            bytes.fromhex("11" * 32),
            assignment_id,
        ),
    )


def _insert_accounting_correction(
    connection: psycopg.Connection[object],
    *,
    delta: Decimal,
    reason: str,
    occurred_at: datetime,
) -> None:
    connection.execute(
        """INSERT INTO router.accounting_corrections (
               correction_id, source_event_id, correction_kind, currency,
               amount_delta, source_name, reason, occurred_at
           ) VALUES (%s, %s, 'invoice', 'USD', %s, 'invoice', %s, %s)""",
        (ACCOUNTING_CORRECTION, ACCOUNTING_EVENT, delta, reason, occurred_at),
    )


def _insert_direct_reservation(
    connection: psycopg.Connection[object],
    *,
    budget_set: str,
    reservation: str,
    amount: Decimal,
    allocation_event_ids: tuple[str, ...] = (),
) -> None:
    with connection.transaction():
        connection.execute(
            """INSERT INTO router.logical_request_budget_sets (
                   id, request_row_id, currency
               ) VALUES (%s, %s, 'USD')""",
            (budget_set, REQUEST_ROW_ID),
        )
        connection.execute(
            """INSERT INTO router.budget_candidate_reservations (
                   id, budget_set_id, reservation_key, candidate_id, candidate_kind,
                   request_fingerprint, estimated_amount, reserved_amount, created_at
               ) VALUES (
                   %s, %s, %s, %s, 'provider_route', decode(repeat('11', 32), 'hex'),
                   %s, %s, %s
               )""",
            (
                reservation,
                budget_set,
                reservation,
                CANDIDATE_ONE,
                amount,
                amount,
                NOW,
            ),
        )
        scope_ids = (
            GLOBAL_BUDGET,
            SERVICE_BUDGET,
            WORKSPACE_BUDGET,
            ASSIGNMENT_BUDGET,
        )
        for event_id, scope_id in zip(allocation_event_ids, scope_ids, strict=False):
            connection.execute(
                """INSERT INTO router.budget_reservation_allocations (
                       reservation_id, budget_scope_id, reserved_amount
                   ) VALUES (%s, %s, %s)""",
                (reservation, scope_id, amount),
            )
            connection.execute(
                """INSERT INTO router.budget_ledger_entries (
                       event_id, reservation_id, budget_scope_id,
                       event_kind, amount, occurred_at
                   ) VALUES (%s, %s, %s, 'reservation', %s, %s)""",
                (event_id, reservation, scope_id, amount, NOW),
            )


def _insert_direct_host_ceiling(
    connection: psycopg.Connection[object],
    *,
    scope_id: str,
    revision: str,
    operation_id: str,
    operation_amount: Decimal | None,
    expected_revision: str | None,
) -> None:
    with connection.transaction():
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id, currency,
                   hard_limit, effective_at, host_ceiling_revision
               ) VALUES (%s, 'host_ceiling', %s, %s, 'USD', 10, %s, %s)""",
            (scope_id, SERVICE_ID, WORKSPACE_ID, NOW, revision),
        )
        connection.execute(
            """INSERT INTO router.workspace_budget_ceilings (
                   service_id, workspace_id, budget_scope_id, amount,
                   currency, revision, operation_id, effective_at
               ) VALUES (%s, %s, %s, 10, 'USD', %s, %s, %s)""",
            (
                SERVICE_ID,
                WORKSPACE_ID,
                scope_id,
                revision,
                operation_id,
                NOW,
            ),
        )
        if operation_amount is not None:
            connection.execute(
                """INSERT INTO router.audit_events (
                       event_id, audit_class, actor_kind, actor_id,
                       authority_class, service_id, workspace_id, action,
                       permission_result, safe_details, occurred_at
                   ) VALUES (
                       %s, 'security', 'service', %s, 'service', %s, %s,
                       'budget_ceiling.write', 'permitted',
                       '{"resource_type":"workspace_budget_ceiling"}', %s
                   )""",
                (
                    operation_id,
                    SERVICE_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    NOW,
                ),
            )
            connection.execute(
                """INSERT INTO router.budget_ceiling_operations (
                       operation_id, service_id, workspace_id, actor_id,
                       idempotency_key, request_fingerprint,
                       expected_revision, resulting_revision, amount,
                       currency, reason, audit_event_id, effective_at
                   ) VALUES (
                       %s, %s, %s, %s, 'direct-sql-ceiling-key',
                       %s, %s, %s, %s, 'USD', 'Direct SQL test.', %s, %s
                   )""",
                (
                    operation_id,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    SERVICE_ID,
                    bytes.fromhex("22" * 32),
                    expected_revision,
                    revision,
                    operation_amount,
                    operation_id,
                    NOW,
                ),
            )


def _insert_service_assignment_limit(
    connection: psycopg.Connection[object], amount: Decimal
) -> None:
    connection.execute(
        """INSERT INTO router.budget_scopes (
               id, scope_kind, service_id, assignment_id,
               currency, hard_limit
           ) VALUES (%s, 'assignment', %s, %s, 'USD', %s)""",
        (ASSIGNMENT_BUDGET, SERVICE_ID, FIXTURE_ASSIGNMENT_ID, amount),
    )
    _record_direct_budget_operations(connection)


def test_ceiling_revision_idempotency_audit_and_authority(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    created = repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(75),
        currency="USD",
        expected_revision=None,
        idempotency_key="workspace-ceiling-key-1",
        reason="Host allocation.",
        now=NOW,
    )
    replay = repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(75),
        currency="USD",
        expected_revision=None,
        idempotency_key="workspace-ceiling-key-1",
        reason="Host allocation.",
        now=NOW,
    )
    assert replay == created
    assert (
        repository.get_host_ceiling(
            _ceiling("budget_ceiling.read", mutation=False),
            service_id=SERVICE_ID,
            workspace_id=WORKSPACE_ID,
        )
        == created
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT action, safe_details FROM router.audit_events WHERE event_id = %s",
            (created.operation_id,),
        ).fetchone() == (
            "budget_ceiling.write",
            {"resource_type": "workspace_budget_ceiling"},
        )
    with pytest.raises(BudgetError) as error:
        repository.put_host_ceiling(
            _ceiling("budget_ceiling.write", mutation=True),
            service_id=SERVICE_ID,
            workspace_id=WORKSPACE_ID,
            amount=Decimal(74),
            currency="USD",
            expected_revision=created.revision,
            idempotency_key="workspace-ceiling-key-1",
            reason="Changed replay.",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT


def test_hierarchy_reservation_warning_reconcile_and_late_correction(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(
            connection,
            amount=Decimal(40),
            occurred_at=NOW,
            budget_scope_id=WORKSPACE_BUDGET,
        )
        _insert_accounting_correction(
            connection,
            delta=Decimal(25),
            reason="Late provider invoice.",
            occurred_at=NOW + timedelta(days=1),
        )
    repository = PostgresBudgetRepository(database_url)
    reserved = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(42),
        reserved_amount=Decimal(45),
        currency="USD",
        maximum_cost=Decimal(100),
        now=NOW,
    )
    assert reserved.state is ReservationState.RESERVED
    assert reserved.accounting_scope_id == WORKSPACE_BUDGET
    assert reserved.external_effects_permitted
    workspace = next(
        item
        for item in reserved.summaries
        if item.scope_kind is BudgetScopeKind.WORKSPACE
    )
    assert workspace.state is EnforcementState.WARNING
    assert (
        repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
            estimated_amount=Decimal(42),
            reserved_amount=Decimal(45),
            currency="USD",
            maximum_cost=Decimal(100),
            now=NOW,
        ).external_effects_permitted
        is False
    )
    assert not repository.reconcile(
        _system("budget.reconcile"),
        reserved.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(40),
        now=NOW,
    )
    summary = repository.summary(
        _budget("budget.read"),
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
        now=NOW,
    )
    assert summary.reserved.amount == 0
    assert summary.used.amount == 40
    assert summary.remaining.amount == 20
    assert not repository.append_correction(
        _system("budget.correct"),
        reserved.reservation_id or "",
        correction_id="0198a080-0000-7000-8000-000000000209",
        accounting_correction_id=ACCOUNTING_CORRECTION,
        amount_delta=Decimal(25),
        reason="Late provider invoice.",
        now=NOW + timedelta(days=1),
    )
    blocked = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_TWO,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        maximum_cost=Decimal(100),
        more_candidates=False,
        now=NOW + timedelta(days=1),
    )
    assert blocked.state is ReservationState.EXHAUSTED


def test_late_usage_above_reservation_is_recorded_and_blocks_admission(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection, workspace_limit=Decimal(50))
        _insert_accounting_fact(connection, amount=Decimal(55), occurred_at=NOW)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(35),
        reserved_amount=Decimal(40),
        currency="USD",
        now=NOW,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(55),
        now=NOW,
    )

    summary = repository.summary(
        _budget("budget.read"),
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
        now=NOW,
    )
    assert summary.used.amount == 55
    assert summary.state is EnforcementState.EXHAUSTED
    blocked = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_TWO,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        more_candidates=False,
        now=NOW,
    )
    assert blocked.state is ReservationState.EXHAUSTED


def test_fallback_shares_logical_maximum_and_candidate_can_be_skipped(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(
            connection,
            amount=Decimal(20),
            occurred_at=NOW,
            candidate_id=CANDIDATE_TWO,
        )
    repository = PostgresBudgetRepository(database_url)
    expensive = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        estimated_amount=Decimal(55),
        reserved_amount=Decimal(55),
        currency="USD",
        maximum_cost=Decimal(50),
        now=NOW,
    )
    assert expensive.state is ReservationState.SKIPPED
    assert expensive.rejected_scope is BudgetScopeKind.LOGICAL_REQUEST
    cheap = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_TWO,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(20),
        reserved_amount=Decimal(20),
        currency="USD",
        maximum_cost=Decimal(50),
        now=NOW,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        cheap.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(20),
        now=NOW,
    )
    third = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id="0198a080-0000-7000-8000-000000000210",
        estimated_amount=Decimal(31),
        reserved_amount=Decimal(31),
        currency="USD",
        maximum_cost=Decimal(50),
        more_candidates=False,
        now=NOW,
    )
    assert third.state is ReservationState.EXHAUSTED
    assert third.rejected_scope is BudgetScopeKind.LOGICAL_REQUEST


def test_provider_route_retries_use_distinct_reservation_keys(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    first = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        reservation_key="provider-attempt-1",
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        maximum_cost=Decimal(30),
        now=NOW,
    )
    replay = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        reservation_key="provider-attempt-1",
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        maximum_cost=Decimal(30),
        now=NOW,
    )
    second = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        reservation_key="provider-attempt-2",
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        maximum_cost=Decimal(30),
        now=NOW,
    )

    assert replay.replayed
    assert replay.reservation_id == first.reservation_id
    assert first.accounting_scope_id == ASSIGNMENT_BUDGET
    assert replay.accounting_scope_id == ASSIGNMENT_BUDGET
    assert second.reservation_id != first.reservation_id
    with pytest.raises(BudgetError) as conflict:
        repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            reservation_key="provider-attempt-1",
            estimated_amount=Decimal(11),
            reserved_amount=Decimal(11),
            currency="USD",
            maximum_cost=Decimal(30),
            now=NOW,
        )
    assert conflict.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT
    for embedding, more_candidates in ((True, True), (False, False)):
        with pytest.raises(BudgetError) as policy_conflict:
            repository.reserve_candidate(
                _system("budget.reserve"),
                request_row_id=REQUEST_ROW_ID,
                candidate_id=CANDIDATE_ONE,
                reservation_key="provider-attempt-1",
                estimated_amount=Decimal(10),
                reserved_amount=Decimal(10),
                currency="USD",
                maximum_cost=Decimal(30),
                embedding=embedding,
                more_candidates=more_candidates,
                now=NOW,
            )
        assert policy_conflict.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT count(*), sum(reserved_amount)
               FROM router.budget_candidate_reservations
               WHERE candidate_id = %s""",
            (CANDIDATE_ONE,),
        ).fetchone() == (2, Decimal(20))


def test_rejected_reservation_key_replays_or_conflicts(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)

    def reject(amount: Decimal) -> ReservationResult:
        return repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            reservation_key="rejected-attempt-1",
            estimated_amount=amount,
            reserved_amount=amount,
            currency="USD",
            more_candidates=False,
            now=NOW,
        )

    first = reject(Decimal(70))
    replay = reject(Decimal(70))
    assert first.state is ReservationState.EXHAUSTED
    assert replay.state is ReservationState.EXHAUSTED
    with pytest.raises(BudgetError) as conflict:
        reject(Decimal(71))
    assert conflict.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize(
    ("period", "used_at", "read_at"),
    [
        ("daily", NOW - timedelta(days=1), NOW),
        ("monthly", datetime(2026, 8, 31, tzinfo=UTC), NOW),
    ],
)
def test_reset_period_uses_utc_window(
    database_url: str, period: str, used_at: datetime, read_at: datetime
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection, reset_period=period)
        _insert_accounting_fact(connection, amount=Decimal(10), occurred_at=used_at)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        now=used_at,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(10),
        now=used_at,
    )
    summary = repository.summary(
        _budget("budget.read"),
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
        now=read_at,
    )
    assert summary.used.amount == 0
    assert summary.remaining.amount == 60


def test_reservation_guard_uses_candidate_time_at_utc_reset_boundary(
    database_url: str,
) -> None:
    before_reset = datetime(2026, 9, 14, 23, 59, 59, tzinfo=UTC)
    after_reset = datetime(2026, 9, 15, tzinfo=UTC)
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(
            connection,
            workspace_limit=Decimal(50),
            reset_period="daily",
        )
        _insert_accounting_fact(
            connection,
            amount=Decimal(50),
            occurred_at=before_reset,
        )
    repository = PostgresBudgetRepository(database_url)
    first = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(50),
        reserved_amount=Decimal(50),
        currency="USD",
        now=before_reset,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        first.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(50),
        now=before_reset,
    )

    second = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_TWO,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        now=after_reset,
    )

    assert second.state is ReservationState.RESERVED


def test_embedding_needs_exact_workspace_budget_and_currency_matches(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, currency, hard_limit
               ) VALUES (%s, 'service', %s, 'USD', 20)""",
            (SERVICE_BUDGET, SERVICE_ID),
        )
        _record_direct_budget_operations(connection)
    repository = PostgresBudgetRepository(database_url)
    with pytest.raises(BudgetError) as error:
        repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            estimated_amount=Decimal(1),
            reserved_amount=Decimal(1),
            currency="USD",
            embedding=True,
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.BUDGET_REQUIRED
    with pytest.raises(BudgetError) as mismatch:
        repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            estimated_amount=Decimal(1),
            reserved_amount=Decimal(1),
            currency="EUR",
            now=NOW,
        )
    assert mismatch.value.code is BudgetErrorCode.CURRENCY_MISMATCH


def test_host_ceiling_without_subordinate_limit_reconciles(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(20),
        currency="USD",
        expected_revision=None,
        idempotency_key="host-only-ceiling-key-1",
        reason="Host-only reconciliation guard.",
        now=NOW,
    )

    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        embedding=True,
        now=NOW,
    )
    assert reservation.accounting_scope_id is not None
    replay = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        embedding=True,
        now=NOW,
    )
    assert replay.replayed
    assert replay.accounting_scope_id == reservation.accounting_scope_id
    with psycopg.connect(database_url) as connection:
        _insert_accounting_fact(
            connection,
            amount=Decimal(1),
            occurred_at=NOW,
            budget_scope_id=reservation.accounting_scope_id,
        )
        _insert_accounting_correction(
            connection,
            delta=Decimal(1),
            reason="Late host-only charge.",
            occurred_at=NOW + timedelta(hours=1),
        )
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(1),
        now=NOW,
    )
    repository.append_correction(
        _system("budget.correct"),
        reservation.reservation_id or "",
        correction_id="0198a080-0000-7000-8000-000000000233",
        accounting_correction_id=ACCOUNTING_CORRECTION,
        amount_delta=Decimal(1),
        reason="Late host-only charge.",
        now=NOW + timedelta(hours=1),
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT scope_kind, service_id::text, workspace_id::text
               FROM router.budget_scopes WHERE id = %s""",
            (reservation.accounting_scope_id,),
        ).fetchone() == ("host_ceiling", SERVICE_ID, WORKSPACE_ID)
        assert connection.execute(
            """SELECT count(*) FROM router.budget_ledger_entries
               WHERE reservation_id = %s AND budget_scope_id = %s""",
            (reservation.reservation_id, reservation.accounting_scope_id),
        ).fetchone() == (4,)


def test_atomic_race_has_one_winner(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        insert_request(connection, SECOND_ROW, SECOND_REQUEST)
        _insert_hierarchy(connection, workspace_limit=Decimal(50))
    repository = PostgresBudgetRepository(database_url)

    def reserve(row: str, candidate: str) -> ReservationState:
        return repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=row,
            candidate_id=candidate,
            estimated_amount=Decimal(40),
            reserved_amount=Decimal(40),
            currency="USD",
            more_candidates=False,
            now=NOW,
        ).state

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        states = tuple(
            executor.map(
                lambda item: reserve(*item),
                ((REQUEST_ROW_ID, CANDIDATE_ONE), (SECOND_ROW, CANDIDATE_TWO)),
            )
        )
    assert sorted(states) == [ReservationState.EXHAUSTED, ReservationState.RESERVED]


def test_reservation_waits_for_first_host_ceiling(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    host_lock = f"host-ceiling:{SERVICE_ID}:{WORKSPACE_ID}"
    with psycopg.connect(database_url) as blocker:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (host_lock,)
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            ceiling = executor.submit(
                repository.put_host_ceiling,
                _ceiling("budget_ceiling.write", mutation=True),
                service_id=SERVICE_ID,
                workspace_id=WORKSPACE_ID,
                amount=Decimal(0),
                currency="USD",
                expected_revision=None,
                idempotency_key="first-ceiling-before-reservation",
                reason="First host allocation.",
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                ceiling.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                assert observer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("budget-hierarchy",),
                ).fetchone() == (False,)
            reservation = executor.submit(
                repository.reserve_candidate,
                _system("budget.reserve"),
                request_row_id=REQUEST_ROW_ID,
                candidate_id=CANDIDATE_ONE,
                estimated_amount=Decimal(1),
                reserved_amount=Decimal(1),
                currency="USD",
                more_candidates=False,
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                reservation.result(timeout=0.1)
            blocker.commit()
            assert ceiling.result(timeout=5).amount.amount == 0
            result = reservation.result(timeout=5)
            assert result.state is ReservationState.EXHAUSTED
            assert result.rejected_scope is BudgetScopeKind.HOST_CEILING
        finally:
            blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_reservation_waits_for_first_applicable_limit(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    idempotency_key = "first-limit-before-reservation"
    operation_lock = f"budget-limit:{SERVICE_ID}:{idempotency_key}"
    with psycopg.connect(database_url) as blocker:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (operation_lock,),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            limit = executor.submit(
                repository.put_limit,
                _budget("budget.write", mutation=True),
                BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
                hard_limit=Decimal(0),
                currency="USD",
                warning_threshold=None,
                reset_period=ResetPeriod.NONE,
                expected_revision="0",
                idempotency_key=idempotency_key,
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                limit.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                assert observer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("budget-hierarchy",),
                ).fetchone() == (False,)
            reservation = executor.submit(
                repository.reserve_candidate,
                _system("budget.reserve"),
                request_row_id=REQUEST_ROW_ID,
                candidate_id=CANDIDATE_ONE,
                estimated_amount=Decimal(1),
                reserved_amount=Decimal(1),
                currency="USD",
                more_candidates=False,
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                reservation.result(timeout=0.1)
            blocker.commit()
            assert limit.result(timeout=5).hard_limit.amount == 0
            result = reservation.result(timeout=5)
            assert result.state is ReservationState.EXHAUSTED
            assert result.rejected_scope is BudgetScopeKind.WORKSPACE
        finally:
            blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_reconciliation_uses_the_same_scope_lock_as_reservation(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(connection, amount=Decimal(10), occurred_at=NOW)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        now=NOW,
    )
    with psycopg.connect(database_url) as blocker:
        blocker.execute(
            "SELECT id FROM router.budget_scopes WHERE id = %s FOR UPDATE",
            (WORKSPACE_BUDGET,),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                repository.reconcile,
                _system("budget.reconcile"),
                reservation.reservation_id or "",
                accounting_event_id=ACCOUNTING_EVENT,
                actual_amount=Decimal(10),
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=0.1)
            blocker.commit()
            assert future.result(timeout=5) is False
        finally:
            blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_limit_write_does_not_lock_ceiling_before_budget_scope(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(60),
        currency="USD",
        expected_revision=None,
        idempotency_key="ceiling-lock-order-key",
        reason="Host allocation.",
        now=NOW,
    )
    with psycopg.connect(database_url) as budget_blocker:
        budget_blocker.execute(
            "SELECT id FROM router.budget_scopes WHERE id = %s FOR UPDATE",
            (WORKSPACE_BUDGET,),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            limit_write = executor.submit(
                repository.put_limit,
                _budget("budget.write", mutation=True),
                BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
                hard_limit=Decimal(55),
                currency="USD",
                warning_threshold=Decimal(40),
                reset_period=ResetPeriod.NONE,
                expected_revision="1",
                idempotency_key="limit-lock-order-key",
                now=NOW + timedelta(seconds=1),
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                limit_write.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                observer.execute("SET LOCAL lock_timeout = '100ms'")
                observer.execute(
                    """SELECT revision FROM router.workspace_budget_ceilings
                       WHERE service_id = %s AND workspace_id = %s FOR UPDATE""",
                    (SERVICE_ID, WORKSPACE_ID),
                )
            budget_blocker.commit()
            assert limit_write.result(timeout=5).hard_limit.amount == 55
        finally:
            budget_blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_committed_correction_blocks_waiting_reservation(database_url: str) -> None:
    correction_time = NOW + timedelta(hours=1)
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(connection, amount=Decimal(45), occurred_at=NOW)
        _insert_accounting_correction(
            connection,
            delta=Decimal(10),
            reason="Late provider charge.",
            occurred_at=correction_time,
        )
    repository = PostgresBudgetRepository(database_url)
    first = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(45),
        reserved_amount=Decimal(45),
        currency="USD",
        now=NOW,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        first.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(45),
        now=NOW,
    )

    with psycopg.connect(database_url) as source_blocker:
        source_blocker.execute(
            "SELECT correction_id FROM router.accounting_corrections "
            "WHERE correction_id = %s FOR UPDATE",
            (ACCOUNTING_CORRECTION,),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            correction = executor.submit(
                repository.append_correction,
                _system("budget.correct"),
                first.reservation_id or "",
                correction_id="0198a080-0000-7000-8000-000000000229",
                accounting_correction_id=ACCOUNTING_CORRECTION,
                amount_delta=Decimal(10),
                reason="Late provider charge.",
                now=correction_time,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                correction.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                observer.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    observer.execute(
                        "SELECT id FROM router.budget_scopes WHERE id = %s FOR UPDATE",
                        (WORKSPACE_BUDGET,),
                    )

            admission = executor.submit(
                repository.reserve_candidate,
                _system("budget.reserve"),
                request_row_id=REQUEST_ROW_ID,
                candidate_id=CANDIDATE_TWO,
                estimated_amount=Decimal(1),
                reserved_amount=Decimal(1),
                currency="USD",
                more_candidates=False,
                now=correction_time,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                admission.result(timeout=0.1)
            source_blocker.commit()
            assert correction.result(timeout=5) is False
            assert admission.result(timeout=5).state is ReservationState.EXHAUSTED
        finally:
            source_blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_direct_sql_preserves_parent_cycle_currency_and_host_ceiling(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        _record_direct_budget_operations(connection)
        connection.commit()
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET),
        )
        _record_direct_budget_operations(connection)
        connection.commit()
        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                """INSERT INTO router.budget_scopes (
                       id, scope_kind, service_id, workspace_id,
                       parent_budget_scope_id, currency, hard_limit
                   ) VALUES (%s, 'workspace', %s, %s, %s, 'EUR', 10)""",
                (
                    WORKSPACE_BUDGET,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    SERVICE_BUDGET,
                ),
            )


@pytest.mark.parametrize(
    ("operation_amount", "expected_revision"),
    [
        (None, None),
        (Decimal(9), None),
        (Decimal(10), "0198a080-0000-7000-8000-000000000237"),
    ],
)
def test_direct_sql_requires_exact_host_ceiling_operation(
    database_url: str,
    operation_amount: Decimal | None,
    expected_revision: str | None,
) -> None:
    scope_id = "0198a080-0000-7000-8000-000000000234"
    revision = "0198a080-0000-7000-8000-000000000235"
    operation_id = "0198a080-0000-7000-8000-000000000236"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error, match="operation is incomplete"),
    ):
        _insert_direct_host_ceiling(
            connection,
            scope_id=scope_id,
            revision=revision,
            operation_id=operation_id,
            operation_amount=operation_amount,
            expected_revision=expected_revision,
        )


def test_negative_correction_cannot_create_reset_period_credit(
    database_url: str,
) -> None:
    use_time = NOW
    correction_time = NOW + timedelta(days=1)
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection, reset_period="daily")
        _insert_accounting_fact(connection, amount=Decimal(10), occurred_at=use_time)
        _insert_accounting_correction(
            connection,
            delta=Decimal(-5),
            reason="Lower invoice.",
            occurred_at=correction_time,
        )
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        now=use_time,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(10),
        now=use_time,
    )
    repository.append_correction(
        _system("budget.correct"),
        reservation.reservation_id or "",
        correction_id="0198a080-0000-7000-8000-000000000217",
        accounting_correction_id=ACCOUNTING_CORRECTION,
        amount_delta=Decimal(-5),
        reason="Lower invoice.",
        now=correction_time,
    )
    summary = repository.summary(
        _budget("budget.read"),
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
        now=correction_time,
    )
    assert summary.corrected.amount == 0
    assert summary.remaining.amount == 60


def test_rejection_is_safe_durable_and_scope_isolated(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    result = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        estimated_amount=Decimal(70),
        reserved_amount=Decimal(70),
        currency="USD",
        more_candidates=False,
        now=NOW,
    )
    assert result.state is ReservationState.EXHAUSTED
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """SELECT request_row_id::text, candidate_id::text, rejected_scope,
                      exhausted
               FROM router.budget_rejections"""
        ).fetchone()
        assert row == (REQUEST_ROW_ID, CANDIDATE_ONE, "workspace", True)
        assert connection.execute(
            """SELECT count(*) FROM router.budget_rejections AS rejection
               JOIN router.logical_requests AS request
                 ON request.row_id = rejection.request_row_id
               WHERE request.service_id = %s""",
            (OTHER_SERVICE_ID,),
        ).fetchone() == (0,)


def test_accounting_source_and_candidate_kind_replay_are_bound(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(connection, amount=Decimal(10), occurred_at=NOW)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        now=NOW,
    )
    with pytest.raises(BudgetError) as kind_error:
        repository.reserve_candidate(
            _system("budget.reserve"),
            request_row_id=REQUEST_ROW_ID,
            candidate_id=CANDIDATE_ONE,
            candidate_kind=BudgetCandidateKind.BUSINESS_TOOL,
            estimated_amount=Decimal(10),
            reserved_amount=Decimal(10),
            currency="USD",
            now=NOW,
        )
    assert kind_error.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(10),
        now=NOW,
    )
    with pytest.raises(BudgetError) as source_error:
        repository.reconcile(
            _system("budget.reconcile"),
            reservation.reservation_id or "",
            accounting_event_id="0198a080-0000-7000-8000-000000000218",
            actual_amount=Decimal(10),
            now=NOW,
        )
    assert source_error.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT


def test_currency_with_ledger_history_cannot_be_relabeled(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        now=NOW,
    )
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        pytest.raises(psycopg.Error, match="currency with financial history"),
    ):
        connection.execute(
            "UPDATE router.budget_scopes SET currency = 'EUR' WHERE id = %s",
            (WORKSPACE_BUDGET,),
        )


def test_host_ceiling_can_decrease_below_existing_router_limit(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    created = repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(70),
        currency="USD",
        expected_revision=None,
        idempotency_key="workspace-ceiling-lower-1",
        reason="Initial host allocation.",
        now=NOW,
    )
    lowered = repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(10),
        currency="USD",
        expected_revision=created.revision,
        idempotency_key="workspace-ceiling-lower-2",
        reason="Lower host allocation.",
        now=NOW + timedelta(seconds=1),
    )
    assert lowered.amount.amount == 10
    with (
        pytest.raises(psycopg.Error, match="currency does not match"),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.workspace_budget_ceilings
                   SET currency = 'EUR', revision = %s, operation_id = %s
                   WHERE service_id = %s AND workspace_id = %s""",
            (
                "0198a080-0000-7000-8000-000000000221",
                "0198a080-0000-7000-8000-000000000222",
                SERVICE_ID,
                WORKSPACE_ID,
            ),
        )
    result = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        estimated_amount=Decimal(11),
        reserved_amount=Decimal(11),
        currency="USD",
        more_candidates=False,
        now=NOW + timedelta(seconds=1),
    )
    assert result.rejected_scope is BudgetScopeKind.HOST_CEILING


def test_limit_write_waits_for_committed_host_ceiling(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    initial = repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(100),
        currency="USD",
        expected_revision=None,
        idempotency_key="ceiling-limit-race-key-1",
        reason="Initial host allocation.",
        now=NOW,
    )

    with psycopg.connect(database_url) as row_blocker:
        row_blocker.execute(
            """SELECT revision FROM router.workspace_budget_ceilings
               WHERE service_id = %s AND workspace_id = %s FOR UPDATE""",
            (SERVICE_ID, WORKSPACE_ID),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            ceiling_write = executor.submit(
                repository.put_host_ceiling,
                _ceiling("budget_ceiling.write", mutation=True),
                service_id=SERVICE_ID,
                workspace_id=WORKSPACE_ID,
                amount=Decimal(40),
                currency="USD",
                expected_revision=initial.revision,
                idempotency_key="ceiling-limit-race-key-2",
                reason="Reduced host allocation.",
                now=NOW + timedelta(seconds=1),
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                ceiling_write.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                assert observer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"host-ceiling:{SERVICE_ID}:{WORKSPACE_ID}",),
                ).fetchone() == (False,)

            limit_write = executor.submit(
                repository.put_limit,
                _budget("budget.write", mutation=True),
                BudgetTarget(
                    BudgetScopeKind.WORKSPACE,
                    SERVICE_ID,
                    WORKSPACE_ID,
                ),
                hard_limit=Decimal(50),
                currency="USD",
                warning_threshold=None,
                reset_period=ResetPeriod.NONE,
                expected_revision="0",
                idempotency_key="ceiling-limit-race-key-3",
                now=NOW + timedelta(seconds=1),
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                limit_write.result(timeout=0.1)
            row_blocker.commit()
            assert ceiling_write.result(timeout=5).amount.amount == 40
            with pytest.raises(BudgetError) as error:
                limit_write.result(timeout=5)
            assert error.value.code is BudgetErrorCode.INVALID_REQUEST
        finally:
            row_blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)

    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.budget_scopes
               WHERE scope_kind = 'workspace' AND service_id = %s
                 AND workspace_id = %s""",
            (SERVICE_ID, WORKSPACE_ID),
        ).fetchone() == (0,)


def test_direct_sql_rejects_wrong_kind_and_sibling_budget_parent(
    database_url: str,
) -> None:
    sibling_budget = "0198a080-0000-7000-8000-000000000219"
    bad_service_budget = "0198a080-0000-7000-8000-000000000220"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, currency, hard_limit
               ) VALUES (%s, 'service', %s, 'USD', 100)""",
            (sibling_budget, OTHER_SERVICE_ID),
        )
        _record_direct_budget_operations(connection)
        connection.commit()
        with (
            pytest.raises(psycopg.Error, match="structural ancestor"),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.budget_scopes (
                       id, scope_kind, service_id, parent_budget_scope_id,
                       currency, hard_limit
                   ) VALUES (%s, 'service', %s, %s, 'USD', 10)""",
                (bad_service_budget, SERVICE_ID, sibling_budget),
            )


def test_direct_sql_cannot_move_a_budget_scope_or_reuse_its_revision(
    database_url: str,
) -> None:
    second_workspace = "0198a080-0000-7000-8000-000000000234"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        connection.execute(
            """INSERT INTO router.workspaces (
                   id, service_id, caller_reference, creation_idempotency_key,
                   creation_fingerprint
               ) VALUES (
                   %s, %s, 'second', 'second-workspace',
                   decode(repeat('55', 32), 'hex')
               )""",
            (second_workspace, SERVICE_ID),
        )

    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error, match="scope identity is immutable"),
        connection.transaction(),
    ):
        connection.execute(
            "UPDATE router.budget_scopes SET workspace_id = %s WHERE id = %s",
            (second_workspace, WORKSPACE_BUDGET),
        )

    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
        connection.transaction(),
    ):
        connection.execute(
            """INSERT INTO router.budget_limit_operations (
                   operation_id, budget_scope_id, actor_id, idempotency_key,
                   request_fingerprint, expected_revision, resulting_revision,
                   hard_limit, warning_threshold, currency, reset_period,
                   audit_event_id, effective_at
               ) SELECT %s, id, 'direct-test', 'reuse-budget-revision',
                        decode(repeat('66', 32), 'hex'), revision, revision,
                        hard_limit, warning_threshold, currency, reset_period,
                        %s, effective_at
                 FROM router.budget_scopes WHERE id = %s""",
            (
                "0198a080-0000-7000-8000-000000000235",
                "0198a080-0000-7000-8000-000000000235",
                WORKSPACE_BUDGET,
            ),
        )


def test_service_limit_insertion_reparents_existing_workspace_budget(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id,
                   parent_budget_scope_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, %s, 'USD', 60)""",
            (WORKSPACE_BUDGET, SERVICE_ID, WORKSPACE_ID, GLOBAL_BUDGET),
        )
        _record_direct_budget_operations(connection)
    repository = PostgresBudgetRepository(database_url)
    limit = repository.put_limit(
        _service_budget_write(),
        BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID),
        hard_limit=Decimal(80),
        currency="USD",
        warning_threshold=Decimal(70),
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="service-budget-create-key",
        now=NOW,
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT parent_budget_scope_id::text FROM router.budget_scopes WHERE id = %s",
            (WORKSPACE_BUDGET,),
        ).fetchone() == (limit.scope_id,)


@pytest.mark.parametrize(
    "target",
    [
        BudgetTarget(
            BudgetScopeKind.SERVICE,
            "0198a080-0000-7000-8000-000000000099",
        ),
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, OTHER_WORKSPACE_ID),
    ],
    ids=["unknown-service", "cross-service-workspace"],
)
def test_limit_write_rejects_an_unknown_exact_scope(
    database_url: str, target: BudgetTarget
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
    repository = PostgresBudgetRepository(database_url)
    with pytest.raises(BudgetError) as error:
        repository.put_limit(
            _administrator_budget_write(target),
            target,
            hard_limit=Decimal(20),
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key=f"unknown-budget-scope-{target.kind.value}",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("target", "table", "identity_column", "identity"),
    [
        (
            BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID),
            "services",
            "id",
            SERVICE_ID,
        ),
        (
            BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
            "workspaces",
            "id",
            WORKSPACE_ID,
        ),
    ],
    ids=["service", "workspace"],
)
def test_limit_write_allows_a_disabled_exact_scope(
    database_url: str,
    target: BudgetTarget,
    table: str,
    identity_column: str,
    identity: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            f"""UPDATE router.{table}
                SET state = 'disabled', state_revision = state_revision + 1
                WHERE {identity_column} = %s""",  # noqa: S608
            (identity,),
        )
    repository = PostgresBudgetRepository(database_url)
    result = repository.put_limit(
        _administrator_budget_write(target),
        target,
        hard_limit=Decimal(20),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key=f"disabled-budget-scope-{target.kind.value}",
        now=NOW,
    )
    assert result.revision == "1"


@pytest.mark.parametrize(
    ("target", "table", "identity"),
    [
        (
            BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID),
            "services",
            SERVICE_ID,
        ),
        (
            BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID),
            "workspaces",
            WORKSPACE_ID,
        ),
    ],
    ids=["service", "workspace"],
)
def test_limit_write_rejects_a_retired_exact_scope(
    database_url: str, target: BudgetTarget, table: str, identity: str
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            f"""UPDATE router.{table}
                SET state = 'retired', retired_at = %s,
                    state_revision = state_revision + 1
                WHERE id = %s""",  # noqa: S608
            (NOW, identity),
        )
    repository = PostgresBudgetRepository(database_url)
    with pytest.raises(BudgetError) as error:
        repository.put_limit(
            _administrator_budget_write(target),
            target,
            hard_limit=Decimal(20),
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key=f"retired-budget-scope-{target.kind.value}",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.NOT_FOUND


def test_existing_limit_rejects_a_currency_change_without_history(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
    repository = PostgresBudgetRepository(database_url)
    target = BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID)
    repository.put_limit(
        _administrator_budget_write(target),
        target,
        hard_limit=Decimal(20),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="original-service-budget-currency",
        now=NOW,
    )
    with pytest.raises(BudgetError) as error:
        repository.put_limit(
            _administrator_budget_write(target),
            target,
            hard_limit=Decimal(20),
            currency="EUR",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="1",
            idempotency_key="changed-service-budget-currency",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.CURRENCY_MISMATCH


def test_new_limit_rejects_an_eligible_bound_route_price_currency(
    database_url: str,
) -> None:
    price_version_id = "0198a080-0000-7000-8000-000000000236"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        seed_request_target(connection)
        connection.execute(
            """INSERT INTO router.route_price_versions (
                   id, provider_model_route_id, version_number, currency, status
               ) VALUES (%s, %s, 1, 'USD', 'current')""",
            (price_version_id, FIXTURE_ROUTE_ID),
        )
        connection.execute(
            """INSERT INTO router.configuration_price_bindings (
                   configuration_revision_id, provider_model_route_id,
                   price_version_id
               ) VALUES (%s, %s, %s)""",
            (CONFIGURATION_ID, FIXTURE_ROUTE_ID, price_version_id),
        )
    repository = PostgresBudgetRepository(database_url)
    target = BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID)
    with pytest.raises(BudgetError) as error:
        repository.put_limit(
            _administrator_budget_write(target),
            target,
            hard_limit=Decimal(20),
            currency="EUR",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key="route-price-budget-currency",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.CURRENCY_MISMATCH


def test_service_reparent_updates_compatible_budget_parents(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 90)""",
            (OTHER_SERVICE_BUDGET, OTHER_SERVICE_ID, GLOBAL_BUDGET),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id,
                   parent_budget_scope_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, %s, 'USD', 60)""",
            (WORKSPACE_BUDGET, SERVICE_ID, WORKSPACE_ID, SERVICE_BUDGET),
        )
        _record_direct_budget_operations(connection)
        connection.commit()
        connection.execute(
            """UPDATE router.services
               SET parent_service_id = %s, state_revision = state_revision + 1
               WHERE id = %s""",
            (OTHER_SERVICE_ID, SERVICE_ID),
        )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT id::text, parent_budget_scope_id::text
               FROM router.budget_scopes
               WHERE id IN (%s, %s) ORDER BY id""",
            (SERVICE_BUDGET, WORKSPACE_BUDGET),
        ).fetchall() == [
            (SERVICE_BUDGET, OTHER_SERVICE_BUDGET),
            (WORKSPACE_BUDGET, SERVICE_BUDGET),
        ]


def test_service_reparent_rejects_incompatible_budget_chain(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 50)""",
            (OTHER_SERVICE_BUDGET, OTHER_SERVICE_ID, GLOBAL_BUDGET),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET),
        )
        _record_direct_budget_operations(connection)
        connection.commit()
        with (
            pytest.raises(psycopg.Error, match="parent budget"),
            connection.transaction(),
        ):
            connection.execute(
                """UPDATE router.services
                   SET parent_service_id = %s,
                       state_revision = state_revision + 1
                   WHERE id = %s""",
                (OTHER_SERVICE_ID, SERVICE_ID),
            )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT parent_service_id FROM router.services WHERE id = %s",
            (SERVICE_ID,),
        ).fetchone() == (None,)


def test_external_tool_accounting_uses_returned_non_assignment_scope(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(2),
        reserved_amount=Decimal(2),
        currency="USD",
        now=NOW,
    )
    assert reservation.accounting_scope_id == WORKSPACE_BUDGET
    with psycopg.connect(database_url) as connection:
        _insert_accounting_fact(
            connection,
            amount=Decimal(2),
            occurred_at=NOW,
            budget_scope_id=reservation.accounting_scope_id,
        )
    assert not repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(2),
        now=NOW,
    )


def test_inherited_service_budget_is_a_valid_accounting_scope(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        connection.execute(
            """UPDATE router.services
               SET parent_service_id = %s, state_revision = state_revision + 1
               WHERE id = %s""",
            (OTHER_SERVICE_ID, SERVICE_ID),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, currency, hard_limit
               ) VALUES (%s, 'service', %s, 'USD', 50)""",
            (OTHER_SERVICE_BUDGET, OTHER_SERVICE_ID),
        )
        _record_direct_budget_operations(connection)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(2),
        reserved_amount=Decimal(2),
        currency="USD",
        now=NOW,
    )
    assert reservation.accounting_scope_id == OTHER_SERVICE_BUDGET
    with psycopg.connect(database_url) as connection:
        _insert_accounting_fact(
            connection,
            amount=Decimal(2),
            occurred_at=NOW,
            budget_scope_id=reservation.accounting_scope_id,
        )
    assert not repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(2),
        now=NOW,
    )


def test_service_assignment_limit_cannot_exceed_workspace_ceiling(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.put_host_ceiling(
        _ceiling("budget_ceiling.write", mutation=True),
        service_id=SERVICE_ID,
        workspace_id=WORKSPACE_ID,
        amount=Decimal(40),
        currency="USD",
        expected_revision=None,
        idempotency_key="service-assignment-ceiling",
        reason="Workspace host allocation.",
        now=NOW,
    )
    target = BudgetTarget(
        BudgetScopeKind.ASSIGNMENT,
        SERVICE_ID,
        assignment_id=FIXTURE_ASSIGNMENT_ID,
    )
    with pytest.raises(BudgetError) as repository_error:
        repository.put_limit(
            _service_budget_write(),
            target,
            hard_limit=Decimal(50),
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key="service-assignment-too-large",
            now=NOW,
        )
    assert repository_error.value.code is BudgetErrorCode.INVALID_REQUEST

    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error, match="service assignment budget"),
        connection.transaction(),
    ):
        _insert_service_assignment_limit(connection, Decimal(50))


def test_workspace_ceiling_rejects_service_assignment_currency(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.put_limit(
        _service_budget_write(),
        BudgetTarget(
            BudgetScopeKind.ASSIGNMENT,
            SERVICE_ID,
            assignment_id=FIXTURE_ASSIGNMENT_ID,
        ),
        hard_limit=Decimal(40),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="service-assignment-before-ceiling",
        now=NOW,
    )
    with pytest.raises(BudgetError) as error:
        repository.put_host_ceiling(
            _ceiling("budget_ceiling.write", mutation=True),
            service_id=SERVICE_ID,
            workspace_id=WORKSPACE_ID,
            amount=Decimal(40),
            currency="EUR",
            expected_revision=None,
            idempotency_key="ceiling-after-service-assignment",
            reason="Workspace host allocation.",
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.CURRENCY_MISMATCH


def test_workspace_and_service_assignment_budgets_require_one_currency(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
    repository = PostgresBudgetRepository(database_url)
    repository.put_limit(
        _service_budget_write(),
        BudgetTarget(
            BudgetScopeKind.ASSIGNMENT,
            SERVICE_ID,
            assignment_id=FIXTURE_ASSIGNMENT_ID,
        ),
        hard_limit=Decimal(40),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="service-assignment-currency",
        now=NOW,
    )
    target = BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE_ID, WORKSPACE_ID)
    with pytest.raises(BudgetError) as repository_error:
        repository.put_limit(
            _budget("budget.write", mutation=True),
            target,
            hard_limit=Decimal(20),
            currency="EUR",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key="workspace-currency-conflict",
            now=NOW,
        )
    assert repository_error.value.code is BudgetErrorCode.CURRENCY_MISMATCH

    with (
        psycopg.connect(database_url) as connection,
        pytest.raises(psycopg.Error, match="co-applicable budget scopes"),
        connection.transaction(),
    ):
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, 'EUR', 20)""",
            (WORKSPACE_BUDGET, SERVICE_ID, WORKSPACE_ID),
        )


def test_limit_api_rejects_parent_below_child_and_currency_change(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    target = BudgetTarget(BudgetScopeKind.SERVICE, SERVICE_ID)
    with pytest.raises(BudgetError) as amount_error:
        repository.put_limit(
            _service_budget_write(),
            target,
            hard_limit=Decimal(50),
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="1",
            idempotency_key="service-parent-below-child",
            now=NOW,
        )
    assert amount_error.value.code is BudgetErrorCode.INVALID_REQUEST
    with pytest.raises(BudgetError) as currency_error:
        repository.put_limit(
            _service_budget_write(),
            target,
            hard_limit=Decimal(80),
            currency="EUR",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="1",
            idempotency_key="service-parent-new-currency",
            now=NOW,
        )
    assert currency_error.value.code is BudgetErrorCode.CURRENCY_MISMATCH


def test_direct_sql_rejects_late_unrelated_scope_allocation(
    database_url: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        estimated_amount=Decimal(1),
        reserved_amount=Decimal(1),
        currency="USD",
        now=NOW,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (
                OTHER_SERVICE_BUDGET,
                OTHER_SERVICE_ID,
                GLOBAL_BUDGET,
            ),
        )
        _record_direct_budget_operations(connection)
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        pytest.raises(psycopg.Error, match="not applicable"),
    ):
        connection.execute(
            """INSERT INTO router.budget_reservation_allocations (
                   reservation_id, budget_scope_id, reserved_amount
               ) VALUES (%s, %s, 1)""",
            (reservation.reservation_id, OTHER_SERVICE_BUDGET),
        )


def test_direct_sql_rejects_duplicate_correction_ledger_entry(
    database_url: str,
) -> None:
    correction_time = NOW + timedelta(hours=1)
    budget_correction_id = "0198a080-0000-7000-8000-000000000231"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
        _insert_accounting_fact(connection, amount=Decimal(10), occurred_at=NOW)
        _insert_accounting_correction(
            connection,
            delta=Decimal(1),
            reason="Late provider charge.",
            occurred_at=correction_time,
        )
    repository = PostgresBudgetRepository(database_url)
    reservation = repository.reserve_candidate(
        _system("budget.reserve"),
        request_row_id=REQUEST_ROW_ID,
        candidate_id=CANDIDATE_ONE,
        candidate_kind=BudgetCandidateKind.EXTERNAL_TOOL,
        estimated_amount=Decimal(10),
        reserved_amount=Decimal(10),
        currency="USD",
        now=NOW,
    )
    repository.reconcile(
        _system("budget.reconcile"),
        reservation.reservation_id or "",
        accounting_event_id=ACCOUNTING_EVENT,
        actual_amount=Decimal(10),
        now=NOW,
    )
    repository.append_correction(
        _system("budget.correct"),
        reservation.reservation_id or "",
        correction_id=budget_correction_id,
        accounting_correction_id=ACCOUNTING_CORRECTION,
        amount_delta=Decimal(1),
        reason="Late provider charge.",
        now=correction_time,
    )
    with psycopg.connect(database_url) as connection:
        usage = connection.execute(
            """SELECT event_id::text FROM router.budget_ledger_entries
               WHERE reservation_id = %s AND budget_scope_id = %s
                 AND event_kind = 'usage'""",
            (reservation.reservation_id, WORKSPACE_BUDGET),
        ).fetchone()
        assert usage is not None
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """INSERT INTO router.budget_ledger_entries (
                       event_id, reservation_id, budget_scope_id, event_kind,
                       amount, source_event_id, source_correction_id, occurred_at
                   ) VALUES (%s, %s, %s, 'correction', 1, %s, %s, %s)""",
                (
                    "0198a080-0000-7000-8000-000000000232",
                    reservation.reservation_id,
                    WORKSPACE_BUDGET,
                    usage[0],
                    budget_correction_id,
                    correction_time,
                ),
            )


def test_direct_sql_cannot_omit_or_overbook_scope_allocations(
    database_url: str,
) -> None:
    budget_set = "0198a080-0000-7000-8000-000000000223"
    reservation = "0198a080-0000-7000-8000-000000000224"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed(connection)
        _insert_hierarchy(connection)
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.Error, match="allocations are incomplete"):
            _insert_direct_reservation(
                connection,
                budget_set=budget_set,
                reservation=reservation,
                amount=Decimal(1),
            )

        event_ids = (
            "0198a080-0000-7000-8000-000000000225",
            "0198a080-0000-7000-8000-000000000226",
            "0198a080-0000-7000-8000-000000000227",
            "0198a080-0000-7000-8000-000000000228",
        )
        with pytest.raises(psycopg.Error, match="exceed hard budget"):
            _insert_direct_reservation(
                connection,
                budget_set=budget_set,
                reservation=reservation,
                amount=Decimal(51),
                allocation_event_ids=event_ids,
            )


def test_budget_migration_rollback_restores_cycle_guard(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        migrate(connection, target=10)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET),
        )
        with pytest.raises(psycopg.Error):
            connection.execute(
                "UPDATE router.budget_scopes SET parent_budget_scope_id = %s WHERE id = %s",
                (SERVICE_BUDGET, GLOBAL_BUDGET),
            )


def test_budget_migration_rollback_restores_legacy_parent_link(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=10)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 100)""",
            (GLOBAL_BUDGET,),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, parent_budget_scope_id,
                   currency, hard_limit
               ) VALUES (%s, 'service', %s, %s, 'USD', 80)""",
            (SERVICE_BUDGET, SERVICE_ID, GLOBAL_BUDGET),
        )
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id,
                   parent_budget_scope_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, %s, 'USD', 60)""",
            (WORKSPACE_BUDGET, SERVICE_ID, WORKSPACE_ID, GLOBAL_BUDGET),
        )

        migrate(connection)
        assert connection.execute(
            "SELECT parent_budget_scope_id::text FROM router.budget_scopes "
            "WHERE id = %s",
            (WORKSPACE_BUDGET,),
        ).fetchone() == (SERVICE_BUDGET,)

        migrate(connection, target=10)
        assert connection.execute(
            "SELECT parent_budget_scope_id::text FROM router.budget_scopes "
            "WHERE id = %s",
            (WORKSPACE_BUDGET,),
        ).fetchone() == (GLOBAL_BUDGET,)

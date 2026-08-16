"""Central fenced budget allowance integration tests."""
# ruff: noqa: D103, FBT003, PLR0913, PLR2004

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    AllowanceBatch,
    AllowanceFinal,
    AllowanceRequest,
    BudgetError,
    BudgetErrorCode,
    BudgetScopeKind,
    BudgetTarget,
    PostgresAllowanceRepository,
    PostgresBudgetRepository,
    ResetPeriod,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    ErrorScope,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
    TerminalError,
    TerminalErrorClass,
)
from llmrouter_backend.lifecycle import PostgresLifecycleRepository

from .helpers import (
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

NOW = datetime(2026, 9, 15, 10, tzinfo=UTC)
GLOBAL_SCOPE = "0198a080-0000-7000-8000-000000000320"
NODE = "0198a080-0000-7000-8000-000000000321"
OTHER_NODE = "0198a080-0000-7000-8000-000000000322"
PARENT_SCOPE = "0198a080-0000-7000-8000-000000000323"


def _system(operation: str, *, actor: str = "allowance-authority") -> RequestContext:
    return RequestContext(
        operation,
        PrincipalKind.SYSTEM,
        actor,
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        operation,
        Scope(),
        NOW,
        None,
        True,
    )


def _administrator(
    operation: str,
    *,
    request_id: str,
    service_id: str | None = None,
) -> RequestContext:
    return RequestContext(
        request_id,
        PrincipalKind.ADMINISTRATOR,
        "issuer:allowance-administrator",
        AuthorityClass.GLOBAL_ADMINISTRATOR,
        AuthorityPath.GLOBAL_ADMINISTRATION,
        None,
        operation,
        Scope(service_id),
        NOW,
        NOW,
        True,
    )


def _seed(database_url: str, *, limit: Decimal = Decimal(20)) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=10)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', %s)""",
            (GLOBAL_SCOPE, limit),
        )
        migrate(connection)


def _seed_scoped(
    database_url: str,
    *,
    global_budget: bool = True,
    parent_budget: bool = False,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=10)
        seed_scope(connection)
        seed_request_target(connection)
        if global_budget:
            connection.execute(
                """INSERT INTO router.budget_scopes (
                       id, scope_kind, currency, hard_limit
                   ) VALUES (%s, 'global', 'USD', 20)""",
                (GLOBAL_SCOPE,),
            )
        else:
            connection.execute(
                """INSERT INTO router.budget_scopes (
                       id, scope_kind, service_id, currency, hard_limit
                   ) VALUES (%s, 'service', %s, 'USD', 20)""",
                (PARENT_SCOPE, SERVICE_ID),
            )
        if parent_budget:
            connection.execute(
                """INSERT INTO router.budget_scopes (
                       id, scope_kind, service_id, parent_budget_scope_id,
                       currency, hard_limit
                   ) VALUES (%s, 'service', %s, %s, 'USD', 20)""",
                (PARENT_SCOPE, OTHER_SERVICE_ID, GLOBAL_SCOPE),
            )
        migrate(connection)


def _issue_scoped(
    database_url: str,
    *,
    requests: tuple[AllowanceRequest, ...],
    service_id: str = SERVICE_ID,
    workspace_id: str | None = WORKSPACE_ID,
    assignment_id: str | None = FIXTURE_ASSIGNMENT_ID,
) -> AllowanceBatch:
    return PostgresAllowanceRepository(database_url).issue(
        _system("budget.allowance.issue"),
        owner_node_id=NODE,
        lease_generation=1,
        service_id=service_id,
        workspace_id=workspace_id,
        assignment_id=assignment_id,
        currency="USD",
        requests=requests,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        safety_until=NOW + timedelta(minutes=3),
        idempotency_key=str(uuid.uuid4()),
    )


def _issue(
    repository: PostgresAllowanceRepository,
    *,
    generation: int,
    owner: str = NODE,
    amount: Decimal = Decimal(6),
    issued_at: datetime = NOW,
    lineage_id: str | None = None,
) -> AllowanceBatch:
    return repository.issue(
        _system("budget.allowance.issue"),
        owner_node_id=owner,
        lease_generation=generation,
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        currency="USD",
        requests=(AllowanceRequest(GLOBAL_SCOPE, amount, Decimal(2)),),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=2),
        safety_until=issued_at + timedelta(minutes=3),
        idempotency_key=str(uuid.uuid4()),
        lineage_id=lineage_id,
    )


def test_renewal_overlap_is_reserved_and_old_generation_can_finalize(
    database_url: str,
) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    old = _issue(repository, generation=1)
    new = _issue(repository, generation=2, lineage_id=old.lineage_id)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, NOW),
        ).fetchone() == (Decimal(12),)
    old_lease = old.leases[0]
    repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        old.batch_id,
        (AllowanceFinal(old_lease.lease_id, NODE, 1, Decimal(4), Decimal(2)),),
        reconciliation_id=str(uuid.uuid4()),
        now=NOW + timedelta(minutes=1),
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, NOW + timedelta(minutes=1)),
        ).fetchone() == (Decimal(10),)
    assert new.leases[0].lease_generation == 2


def test_independent_node_lineages_can_hold_bounded_grants(database_url: str) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    first = _issue(repository, generation=1, owner=NODE)
    second = _issue(repository, generation=1, owner=OTHER_NODE)
    assert first.lineage_id != second.lineage_id
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, NOW),
        ).fetchone() == (Decimal(12),)


def test_issue_replays_exact_request_and_conflicts_on_changed_payload(
    database_url: str,
) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    key = "allowance-issue-replay"
    request = AllowanceRequest(GLOBAL_SCOPE, Decimal(4), Decimal(1))
    arguments: dict[str, Any] = {
        "owner_node_id": NODE,
        "lease_generation": 1,
        "service_id": None,
        "workspace_id": None,
        "assignment_id": None,
        "currency": "USD",
        "requests": (request,),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=2),
        "safety_until": NOW + timedelta(minutes=3),
        "idempotency_key": key,
    }
    first = repository.issue(_system("budget.allowance.issue"), **arguments)
    assert repository.issue(_system("budget.allowance.issue"), **arguments) == first
    changed = {
        **arguments,
        "requests": (AllowanceRequest(GLOBAL_SCOPE, Decimal(4), Decimal(2)),),
    }
    with pytest.raises(BudgetError) as conflict:
        repository.issue(_system("budget.allowance.issue"), **changed)
    assert conflict.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT


def test_old_central_reservation_reduces_allowance_capacity(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection, target=10)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 20)""",
            (GLOBAL_SCOPE,),
        )
        connection.execute(
            """INSERT INTO router.budget_reservations (
                   id, request_row_id, budget_scope_id, currency,
                   estimated_amount, reserved_amount, created_at
               ) VALUES (%s, %s, %s, 'USD', 9, 10, %s)""",
            (uuid.uuid4(), REQUEST_ROW_ID, GLOBAL_SCOPE, NOW),
        )
        migrate(connection, target=14)
        PostgresExecutionRepository(database_url).transition(
            RequestContext(
                "allowance-migration",
                PrincipalKind.SYSTEM,
                "allowance-migration",
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
            new_state=ExecutionState.FAILED,
            safe_error=TerminalError(
                TerminalErrorClass.ROUTER_INTERNAL,
                ErrorScope.LOGICAL_REQUEST,
                "The legacy test request is closed before migration.",
            ),
        )
        migrate(connection)
    repository = PostgresAllowanceRepository(database_url)
    with pytest.raises(BudgetError) as exhausted:
        _issue(repository, generation=1, amount=Decimal(11))
    assert exhausted.value.code is BudgetErrorCode.BUDGET_EXHAUSTED


def test_stale_owner_generation_early_reclaim_and_double_final_are_rejected(
    database_url: str,
) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    batch = _issue(repository, generation=1)
    with pytest.raises(BudgetError) as stale:
        repository.issue(
            _system("budget.allowance.issue"),
            owner_node_id=NODE,
            lease_generation=1,
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            currency="USD",
            requests=(AllowanceRequest(GLOBAL_SCOPE, Decimal(1), Decimal(0)),),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            safety_until=NOW + timedelta(minutes=3),
            idempotency_key=str(uuid.uuid4()),
            lineage_id=batch.lineage_id,
        )
    assert stale.value.code is BudgetErrorCode.STALE_ALLOWANCE
    with pytest.raises(BudgetError) as owner:
        _issue(
            repository,
            generation=2,
            owner=OTHER_NODE,
            lineage_id=batch.lineage_id,
        )
    assert owner.value.code is BudgetErrorCode.STALE_ALLOWANCE
    lease = batch.leases[0]
    with pytest.raises(BudgetError):
        repository.reclaim(
            _system("budget.allowance.reclaim"),
            batch_id=batch.batch_id,
            reconciliation_id=str(uuid.uuid4()),
            now=NOW,
        )
    final = AllowanceFinal(lease.lease_id, NODE, 1, Decimal(3), Decimal(3))
    repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        batch.batch_id,
        (final,),
        reconciliation_id=str(uuid.uuid4()),
        now=NOW,
    )
    with pytest.raises(BudgetError) as duplicate:
        repository.reconcile(
            _system("budget.allowance.reconcile", actor=NODE),
            batch.batch_id,
            (final,),
            reconciliation_id=str(uuid.uuid4()),
            now=NOW,
        )
    assert duplicate.value.code is BudgetErrorCode.STALE_ALLOWANCE


def test_node_failure_reclaim_is_conservative_and_late_correction_is_bounded(
    database_url: str,
) -> None:
    _seed(database_url, limit=Decimal(10))
    repository = PostgresAllowanceRepository(database_url)
    batch = _issue(repository, generation=1, amount=Decimal(6))
    lease = batch.leases[0]
    repository.reclaim(
        _system("budget.allowance.reclaim"),
        batch_id=batch.batch_id,
        reconciliation_id=str(uuid.uuid4()),
        now=NOW + timedelta(minutes=3),
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, NOW + timedelta(minutes=3)),
        ).fetchone() == (Decimal(6),)
    correction_id = str(uuid.uuid4())
    assert not repository.append_correction(
        _system("budget.allowance.correct"),
        lease_id=lease.lease_id,
        correction_id=correction_id,
        amount_delta=Decimal(2),
        reason="Delayed provider usage.",
        now=NOW + timedelta(minutes=4),
    )
    assert repository.append_correction(
        _system("budget.allowance.correct"),
        lease_id=lease.lease_id,
        correction_id=correction_id,
        amount_delta=Decimal(2),
        reason="Delayed provider usage.",
        now=NOW + timedelta(minutes=4),
    )
    with pytest.raises(BudgetError) as replay_conflict:
        repository.append_correction(
            _system("budget.allowance.correct"),
            lease_id=lease.lease_id,
            correction_id=correction_id,
            amount_delta=Decimal(1),
            reason="Changed delayed provider usage.",
            now=NOW + timedelta(minutes=4),
        )
    assert replay_conflict.value.code is BudgetErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(BudgetError):
        repository.append_correction(
            _system("budget.allowance.correct"),
            lease_id=lease.lease_id,
            correction_id=str(uuid.uuid4()),
            amount_delta=Decimal("0.000000000000000001"),
            reason="Excess delayed provider usage.",
            now=NOW + timedelta(minutes=4),
        )


def test_direct_sql_rejects_grant_overissue_and_incomplete_ledger(
    database_url: str,
) -> None:
    _seed(database_url, limit=Decimal(5))
    repository = PostgresAllowanceRepository(database_url)
    with pytest.raises(BudgetError) as exhausted:
        _issue(repository, generation=1, amount=Decimal(6))
    assert exhausted.value.code is BudgetErrorCode.BUDGET_EXHAUSTED

    batch = _issue(repository, generation=1, amount=Decimal(5))
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """INSERT INTO router.budget_allowance_batch_reconciliations (
                       reconciliation_id, batch_id, owner_node_id,
                       lease_generation, request_fingerprint, occurred_at, reclaimed
                   ) VALUES (%s, %s, %s, 1, %s, %s, false)""",
                (uuid.uuid4(), batch.batch_id, NODE, bytes(32), NOW),
            )
    direct_batch = uuid.uuid4()
    direct_lease = uuid.uuid4()

    def insert_lease_without_grant() -> None:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """INSERT INTO router.budget_allowance_batches (
                       id, issuer_id, idempotency_key, request_fingerprint,
                       lineage_id, owner_node_id, lease_generation, currency,
                       issued_at, expires_at, safety_until
                   ) VALUES (%s, 'direct-sql', %s, %s, %s, %s, 1, 'USD',
                             %s, %s, %s)""",
                (
                    direct_batch,
                    str(uuid.uuid4()),
                    bytes(32),
                    uuid.uuid4(),
                    OTHER_NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )
            connection.execute(
                """INSERT INTO router.budget_allowance_leases (
                       id, batch_id, budget_scope_id, currency, owner_node_id,
                       lease_generation, issued_amount, maximum_correction_risk,
                       issued_at, expires_at, safety_until
                       ) VALUES (%s, %s, %s, 'USD', %s, 1, 0, 0, %s, %s, %s)""",
                (
                    direct_lease,
                    direct_batch,
                    GLOBAL_SCOPE,
                    OTHER_NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="grant ledger is incomplete"
    ):
        insert_lease_without_grant()


def test_direct_sql_rejects_stale_owner_early_reclaim_and_double_final(
    database_url: str,
) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    batch = _issue(repository, generation=1)
    lease = batch.leases[0]
    batch_columns = """id, lineage_id, owner_node_id, lease_generation, currency,
                       issued_at, expires_at, safety_until"""
    with psycopg.connect(database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                f"""INSERT INTO router.budget_allowance_batches ({batch_columns})
                    VALUES (%s, %s, %s, 1, 'USD', %s, %s, %s)""",  # noqa: S608
                (
                    uuid.uuid4(),
                    batch.lineage_id,
                    NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )
        with pytest.raises(psycopg.errors.SerializationFailure):
            connection.execute(
                f"""INSERT INTO router.budget_allowance_batches ({batch_columns})
                    VALUES (%s, %s, %s, 2, 'USD', %s, %s, %s)""",  # noqa: S608
                (
                    uuid.uuid4(),
                    batch.lineage_id,
                    OTHER_NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                f"""INSERT INTO router.budget_allowance_batches ({batch_columns})
                    VALUES (%s, %s, %s, 2, 'EUR', %s, %s, %s)""",  # noqa: S608
                (
                    uuid.uuid4(),
                    batch.lineage_id,
                    NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.budget_allowance_batch_reconciliations (
                       reconciliation_id, batch_id, owner_node_id,
                       lease_generation, request_fingerprint, occurred_at, reclaimed
                   ) VALUES (%s, %s, %s, 1, %s, %s, true)""",
                (uuid.uuid4(), batch.batch_id, NODE, bytes(32), NOW),
            )
    final = AllowanceFinal(lease.lease_id, NODE, 1, Decimal(4), Decimal(2))
    repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        batch.batch_id,
        (final,),
        reconciliation_id=str(uuid.uuid4()),
        now=NOW,
    )
    with pytest.raises(BudgetError) as duplicate:
        repository.reconcile(
            _system("budget.allowance.reconcile", actor=NODE),
            batch.batch_id,
            (final,),
            reconciliation_id=str(uuid.uuid4()),
            now=NOW,
        )
    assert duplicate.value.code is BudgetErrorCode.STALE_ALLOWANCE


def test_direct_sql_rejects_new_legacy_and_noninitial_lineage(
    database_url: str,
) -> None:
    _seed(database_url)
    columns = """id, lineage_id, owner_node_id, lease_generation, currency,
                 issued_at, expires_at, safety_until, legacy"""
    with psycopg.connect(database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation, match="legacy mode"):
            connection.execute(
                f"""INSERT INTO router.budget_allowance_batches ({columns})
                    VALUES (%s, %s, %s, 1, 'USD', %s, %s, %s, true)""",  # noqa: S608
                (
                    uuid.uuid4(),
                    uuid.uuid4(),
                    NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="generation one"):
            connection.execute(
                f"""INSERT INTO router.budget_allowance_batches ({columns})
                    VALUES (%s, %s, %s, 2, 'USD', %s, %s, %s, false)""",  # noqa: S608
                (
                    uuid.uuid4(),
                    uuid.uuid4(),
                    NODE,
                    NOW,
                    NOW + timedelta(minutes=2),
                    NOW + timedelta(minutes=3),
                ),
            )


@pytest.mark.parametrize("scope_kind", ["service", "workspace", "assignment"])
def test_new_applicable_scope_rejects_outstanding_batch(
    database_url: str,
    scope_kind: str,
) -> None:
    _seed_scoped(database_url)
    _issue_scoped(
        database_url,
        requests=(AllowanceRequest(GLOBAL_SCOPE, Decimal(5), Decimal(1)),),
    )
    values = {
        "service": (SERVICE_ID, None, None),
        "workspace": (SERVICE_ID, WORKSPACE_ID, None),
        "assignment": (SERVICE_ID, WORKSPACE_ID, FIXTURE_ASSIGNMENT_ID),
    }[scope_kind]
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        pytest.raises(
            psycopg.errors.CheckViolation,
            match="topology would invalidate",
        ),
    ):
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id, assignment_id,
                   parent_budget_scope_id, currency, hard_limit
               ) VALUES (%s, %s, %s, %s, %s, %s, 'USD', 10)""",
            (uuid.uuid4(), scope_kind, *values, GLOBAL_SCOPE),
        )


def test_new_global_scope_rejects_outstanding_service_batch(database_url: str) -> None:
    _seed_scoped(database_url, global_budget=False)
    _issue_scoped(
        database_url,
        requests=(AllowanceRequest(PARENT_SCOPE, Decimal(5), Decimal(1)),),
        workspace_id=None,
        assignment_id=None,
    )
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        pytest.raises(
            psycopg.errors.CheckViolation,
            match="topology would invalidate",
        ),
    ):
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, currency, hard_limit
               ) VALUES (%s, 'global', 'USD', 20)""",
            (GLOBAL_SCOPE,),
        )


def test_multi_scope_finalization_is_atomic_and_replays_exactly(
    database_url: str,
) -> None:
    _seed_scoped(database_url)
    service_limit = PostgresBudgetRepository(database_url).put_limit(
        _administrator(
            "budget.write",
            request_id="allowance-multi-scope",
            service_id=SERVICE_ID,
        ),
        BudgetTarget(BudgetScopeKind.SERVICE, service_id=SERVICE_ID),
        hard_limit=Decimal(20),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="allowance-multi-scope-key",
        now=NOW,
    )
    service_scope = service_limit.scope_id
    batch = _issue_scoped(
        database_url,
        requests=(
            AllowanceRequest(GLOBAL_SCOPE, Decimal(5), Decimal(1)),
            AllowanceRequest(service_scope, Decimal(5), Decimal(1)),
        ),
        workspace_id=None,
        assignment_id=None,
    )
    repository = PostgresAllowanceRepository(database_url)
    finals = tuple(
        AllowanceFinal(lease.lease_id, NODE, 1, Decimal(3), Decimal(2))
        for lease in batch.leases
    )
    reconciliation_id = str(uuid.uuid4())
    with pytest.raises(BudgetError) as incomplete:
        repository.reconcile(
            _system("budget.allowance.reconcile", actor=NODE),
            batch.batch_id,
            finals[:1],
            reconciliation_id=reconciliation_id,
            now=NOW + timedelta(minutes=1),
        )
    assert incomplete.value.code is BudgetErrorCode.STALE_ALLOWANCE
    assert not repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        batch.batch_id,
        finals,
        reconciliation_id=reconciliation_id,
        now=NOW + timedelta(minutes=1),
    )
    assert repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        batch.batch_id,
        reversed(finals),
        reconciliation_id=reconciliation_id,
        now=NOW + timedelta(minutes=1),
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """SELECT count(*), count(DISTINCT batch_reconciliation_id)
               FROM router.budget_allowance_reconciliations
               WHERE batch_reconciliation_id = %s""",
            (reconciliation_id,),
        ).fetchone() == (2, 1)


def test_service_reparent_rejects_outstanding_inherited_scope_change(
    database_url: str,
) -> None:
    _seed_scoped(database_url, parent_budget=True)
    _issue_scoped(
        database_url,
        requests=(AllowanceRequest(GLOBAL_SCOPE, Decimal(5), Decimal(1)),),
        workspace_id=None,
        assignment_id=None,
    )
    lifecycle = PostgresLifecycleRepository(database_url)
    with pytest.raises(
        psycopg.errors.CheckViolation, match="topology would invalidate"
    ):
        lifecycle.change_service_parent(
            _administrator("service_parent.manage", request_id="allowance-reparent"),
            service_id=SERVICE_ID,
            expected_revision="1",
            new_parent_service_id=OTHER_SERVICE_ID,
            reason="Test an allowance topology change.",
        )


def test_delayed_reconciliation_keeps_usage_in_lease_reset_period(
    database_url: str,
) -> None:
    _seed(database_url)
    PostgresBudgetRepository(database_url).put_limit(
        _administrator("budget.write", request_id="allowance-reset"),
        BudgetTarget(BudgetScopeKind.GLOBAL),
        hard_limit=Decimal(20),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.DAILY,
        expected_revision="1",
        idempotency_key="allowance-reset-key-0001",
        now=NOW,
    )
    repository = PostgresAllowanceRepository(database_url)
    batch = _issue(repository, generation=1)
    next_day = NOW + timedelta(days=1)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, next_day),
        ).fetchone() == (Decimal(0),)
    lease = batch.leases[0]
    repository.reconcile(
        _system("budget.allowance.reconcile", actor=NODE),
        batch.batch_id,
        (AllowanceFinal(lease.lease_id, NODE, 1, Decimal(4), Decimal(2)),),
        reconciliation_id=str(uuid.uuid4()),
        now=next_day,
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT router.allowance_scope_consumed(%s, %s)",
            (GLOBAL_SCOPE, next_day),
        ).fetchone() == (Decimal(0),)
        assert connection.execute(
            """SELECT DISTINCT occurred_at
               FROM router.budget_allowance_ledger_entries
               WHERE allowance_lease_id = %s""",
            (lease.lease_id,),
        ).fetchall() == [(NOW,)]
    with pytest.raises(BudgetError):
        _issue(
            repository,
            generation=1,
            issued_at=NOW.replace(hour=23, minute=59),
        )


def test_reset_period_change_cannot_cross_outstanding_allowance(
    database_url: str,
) -> None:
    _seed(database_url)
    repository = PostgresAllowanceRepository(database_url)
    _issue(
        repository,
        generation=1,
        issued_at=NOW.replace(hour=23, minute=59),
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="topology would invalidate"
    ):
        PostgresBudgetRepository(database_url).put_limit(
            _administrator("budget.write", request_id="allowance-reset-change"),
            BudgetTarget(BudgetScopeKind.GLOBAL),
            hard_limit=Decimal(20),
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.DAILY,
            expected_revision="1",
            idempotency_key="allowance-reset-change-key",
            now=NOW.replace(hour=23, minute=59),
        )

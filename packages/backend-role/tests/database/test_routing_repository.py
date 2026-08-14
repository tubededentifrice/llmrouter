"""PostgreSQL provider routing, fallback, diagnostic, and recovery tests."""
# ruff: noqa: D103, FBT003, FURB157

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from llmrouter_backend.accounting import UsageComponent, UsageUnit
from llmrouter_backend.admission import (
    AdmissionError,
    AdmissionErrorCode,
    AdmissionRequest,
    FingerprintInput,
    PostgresAdmissionRepository,
    RequestKind,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import PostgresBudgetRepository, ReservationState
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
from llmrouter_backend.routing import (
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    FallbackDecision,
    PostgresRoutingRepository,
    RoutingError,
    RoutingErrorCode,
    SafeFailureEvidence,
)

from .helpers import SERVICE_ID, WORKSPACE_ID
from .test_admission_repository import (
    ROUTE_ID,
    _context,
    _request,
    _seed_admission_target,
    _uuidv7,
)


@pytest.fixture
def repositories(
    database_url: str,
) -> tuple[
    PostgresAdmissionRepository,
    PostgresExecutionRepository,
    PostgresRoutingRepository,
    PostgresBudgetRepository,
]:
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        _seed_admission_target(connection)
    return (
        PostgresAdmissionRepository(database_url),
        PostgresExecutionRepository(database_url),
        PostgresRoutingRepository(database_url),
        PostgresBudgetRepository(database_url),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _budget_context(now: datetime) -> RequestContext:
    return RequestContext(
        "routing-budget",
        PrincipalKind.SYSTEM,
        "routing-budget",
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        "budget.reserve",
        Scope(),
        now,
        None,
        True,
    )


def _grant_context(now: datetime) -> RequestContext:
    return RequestContext(
        "routing-grant",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.CONFIGURATION,
        "diagnostic.grant.create",
        Scope(SERVICE_ID, WORKSPACE_ID),
        now,
        None,
        True,
    )


def _admit_running(
    admission: PostgresAdmissionRepository,
    execution: PostgresExecutionRepository,
    *,
    now: datetime,
    random_bits: int,
) -> str:
    request_id = _uuidv7(now, random_bits)
    admission.admit(_context(), _request(request_id), now=now)
    execution.transition(
        _context(),
        ExecutionTarget(ExecutionKind.MODEL, request_id),
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    return request_id


def _failure(attempt_id: str, detail_code: str) -> AttemptFailure:
    return AttemptFailure(
        TerminalError(
            TerminalErrorClass.TRANSPORT,
            ErrorScope.ATTEMPT,
            "The provider attempt failed.",
        ),
        attempt_id,
        SafeFailureEvidence(detail_code=detail_code),
    )


def _exact_request(request_id: str, bearer: str) -> AdmissionRequest:
    fingerprint = FingerprintInput(
        "model.create",
        1,
        SERVICE_ID,
        WORKSPACE_ID,
        "service-data",
        {
            "api_version": "1",
            "exact_route": ROUTE_ID,
            "messages": [{"role": "user", "content": "Diagnostic test"}],
            "limits": {"logical_timeout_ms": 120000},
            "output": {"format": "text"},
        },
        resolved_exact_route_scope={
            "service_id": SERVICE_ID,
            "workspace_id": WORKSPACE_ID,
            "exact_route_id": ROUTE_ID,
        },
    )
    return AdmissionRequest(
        request_id,
        RequestKind.MODEL,
        fingerprint,
        exact_route_id=ROUTE_ID,
        diagnostic_grant=bearer,
    )


def test_claim_reject_replay_and_inherited_disable_continuation(
    database_url: str,
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, execution, routing, _budget = repositories
    now = _now()
    request_id = _admit_running(admission, execution, now=now, random_bits=21)
    disable_revision = uuid.uuid4()
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.configuration_revisions (
                   id, scope_kind, service_id, workspace_id, revision_number,
                   content, content_sha256, created_by_kind, created_by_id
               ) VALUES (%s, 'workspace', %s, %s, 2,
                         jsonb_build_object(
                             'inherited_disables', jsonb_build_array(
                                 jsonb_build_object(
                                     'resource_kind', 'provider_model_route',
                                     'resource_id', %s::text
                                 )
                             )
                         ), decode(repeat('81', 32), 'hex'), 'service', %s)""",
            (disable_revision, SERVICE_ID, WORKSPACE_ID, ROUTE_ID, SERVICE_ID),
        )
        connection.execute(
            """INSERT INTO router.active_configurations (
                   scope_kind, service_id, workspace_id, revision_id, revision_number
               ) VALUES ('workspace', %s, %s, %s, 2)""",
            (SERVICE_ID, WORKSPACE_ID, disable_revision),
        )

    plan = routing.claim(_context(), request_id=request_id, owner_id="worker-one")
    assert plan.provider_model_route_id == ROUTE_ID
    failure = _failure(plan.attempt_id, "prestart_transport")
    routing.reject_before_start(plan, failure, FallbackDecision.NEXT_CANDIDATE, now=now)
    routing.reject_before_start(plan, failure, FallbackDecision.NEXT_CANDIDATE, now=now)
    with pytest.raises(RoutingError) as conflict:
        routing.reject_before_start(
            plan,
            _failure(plan.attempt_id, "changed_evidence"),
            FallbackDecision.NEXT_CANDIDATE,
            now=now,
        )
    assert conflict.value.code is RoutingErrorCode.CLAIM_CONFLICT
    with pytest.raises(RoutingError) as exhausted:
        routing.claim(_context(), request_id=request_id, owner_id="worker-two")
    assert exhausted.value.code is RoutingErrorCode.NO_CANDIDATE

    with pytest.raises(AdmissionError) as disabled:
        admission.admit(
            _context(),
            _request(_uuidv7(now, 22)),
            now=now,
        )
    assert disabled.value.code is AdmissionErrorCode.ASSIGNMENT_UNAVAILABLE


def test_start_dispatch_finish_usage_and_terminal_recovery(
    database_url: str,
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, execution, routing, budget = repositories
    now = _now()
    request_id = _admit_running(admission, execution, now=now, random_bits=23)
    plan = routing.claim(_context(), request_id=request_id, owner_id="worker-one")
    reservation = budget.reserve_candidate(
        _budget_context(now),
        request_row_id=plan.request_row_id,
        candidate_id=plan.provider_model_route_id,
        reservation_key=plan.reservation_key,
        estimated_amount=Decimal("0.01"),
        reserved_amount=Decimal("0.01"),
        currency="USD",
        maximum_cost=Decimal("1"),
        more_candidates=False,
        now=now,
    )
    assert reservation.state is ReservationState.RESERVED
    assert reservation.reservation_id is not None
    routing.start(plan, budget_reservation_id=reservation.reservation_id)
    routing.start(plan, budget_reservation_id=reservation.reservation_id)
    assert routing.started(plan, budget_reservation_id=reservation.reservation_id)
    with pytest.raises(RoutingError) as changed_start:
        routing.start(
            replace(plan, route_generation=plan.route_generation + 1),
            budget_reservation_id=reservation.reservation_id,
        )
    assert changed_start.value.code is RoutingErrorCode.CLAIM_CONFLICT

    with (
        psycopg.connect(database_url) as connection,
        connection.transaction(),
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(
            """INSERT INTO router.routing_attempt_usage_reports (
                   attempt_id, usage_components
               ) VALUES (%s, '[{"unit":"input_token","quantity":"1"}]')""",
            (plan.attempt_id,),
        )

    assert routing.dispatch(plan, owner_id="worker-one")
    assert not routing.dispatch(plan, owner_id="worker-one")
    with pytest.raises(RoutingError) as wrong_owner:
        routing.dispatch(plan, owner_id="worker-two")
    assert wrong_owner.value.code is RoutingErrorCode.CLAIM_CONFLICT

    result = AdapterResult(
        AttemptOutcome.SUCCEEDED,
        usage=(UsageComponent(UsageUnit.INPUT_TOKEN, Decimal("1E-8")),),
    )
    assert routing.finish(plan, result, FallbackDecision.SUCCEEDED, now=now) == result
    assert routing.finish(plan, result, FallbackDecision.SUCCEEDED, now=now) == result
    with pytest.raises(RoutingError) as changed_usage:
        routing.finish(
            plan,
            AdapterResult(
                AttemptOutcome.SUCCEEDED,
                usage=(UsageComponent(UsageUnit.INPUT_TOKEN, Decimal("2E-8")),),
            ),
            FallbackDecision.SUCCEEDED,
            now=now,
        )
    assert changed_usage.value.code is RoutingErrorCode.CLAIM_CONFLICT

    recovered = routing.pending_accounting(_context(), request_id=request_id)
    assert recovered is not None
    recovered_plan, recovered_result, accounting_complete = recovered
    assert recovered_plan.attempt_id == plan.attempt_id
    assert recovered_result == result
    assert not accounting_complete
    with psycopg.connect(database_url) as connection:
        usage = connection.execute(
            """SELECT usage_components
               FROM router.routing_attempt_usage_reports WHERE attempt_id = %s""",
            (plan.attempt_id,),
        ).fetchone()
    assert usage == ([{"unit": "input_token", "quantity": "0.00000001"}],)


def test_workspace_disable_after_admission_still_allows_first_claim(
    database_url: str,
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, execution, routing, _budget = repositories
    now = _now()
    request_id = _admit_running(admission, execution, now=now, random_bits=27)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.workspaces
               SET state = 'disabled', state_revision = state_revision + 1
               WHERE id = %s AND service_id = %s""",
            (WORKSPACE_ID, SERVICE_ID),
        )

    plan = routing.claim(_context(), request_id=request_id, owner_id="worker-one")

    assert not plan.recovery_only
    assert not plan.started
    assert plan.recovery_failure is None


def test_terminal_prestart_decision_replays_and_cannot_start(
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, execution, routing, budget = repositories
    now = _now()
    request_id = _admit_running(admission, execution, now=now, random_bits=28)
    plan = routing.claim(_context(), request_id=request_id, owner_id="worker-one")
    reservation = budget.reserve_candidate(
        _budget_context(now),
        request_row_id=plan.request_row_id,
        candidate_id=plan.provider_model_route_id,
        reservation_key=plan.reservation_key,
        estimated_amount=Decimal("0.01"),
        reserved_amount=Decimal("0.01"),
        currency="USD",
        maximum_cost=Decimal("1"),
        more_candidates=False,
        now=now,
    )
    assert reservation.reservation_id is not None
    failure = AttemptFailure(
        TerminalError(
            TerminalErrorClass.POLICY,
            ErrorScope.LOGICAL_REQUEST,
            "The request cannot continue.",
        ),
        request_id,
        SafeFailureEvidence(detail_code="request_policy"),
    )
    routing.reject_before_start(
        plan, failure, FallbackDecision.STOP_REQUEST, now=now
    )

    replay = routing.pending_accounting(_context(), request_id=request_id)

    assert replay is not None
    replay_plan, replay_result, accounting_complete = replay
    assert replace(
        replay_plan, recovery_only=False, recovery_failure=None
    ) == plan
    assert replay_plan.recovery_failure is not None
    assert replay_plan.recovery_failure.error.error_class is TerminalErrorClass.POLICY
    assert replay_plan.recovery_failure.evidence.detail_code == "request_policy"
    assert replay_result.outcome is AttemptOutcome.FAILED
    assert replay_result.failure == replay_plan.recovery_failure
    assert accounting_complete
    with pytest.raises(psycopg.Error):
        routing.start(plan, budget_reservation_id=reservation.reservation_id)


def test_deadline_before_first_claim_is_durable_and_replayable(
    database_url: str,
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, execution, routing, _budget = repositories
    admitted_at = _now() - timedelta(minutes=15, seconds=1)
    request_id = _admit_running(
        admission, execution, now=admitted_at, random_bits=29
    )

    first = routing.claim(_context(), request_id=request_id, owner_id="worker-one")
    second = routing.claim(_context(), request_id=request_id, owner_id="worker-two")
    replay = routing.pending_accounting(_context(), request_id=request_id)

    assert first == second
    assert first.request_terminal
    assert first.recovery_only
    assert first.recovery_failure is not None
    assert first.recovery_failure.error.error_class is TerminalErrorClass.TIMEOUT
    assert first.recovery_failure.evidence.detail_code == "logical_deadline"
    assert replay is not None
    assert replay[0] == first
    assert replay[1] == AdapterResult(AttemptOutcome.FAILED, first.recovery_failure)
    assert replay[2]
    with psycopg.connect(database_url) as connection:
        durable = connection.execute(
            """SELECT count(*) FROM router.routing_request_terminal_decisions
               WHERE request_row_id = %s""",
            (first.request_row_id,),
        ).fetchone()
    assert durable == (1,)


def test_diagnostic_grant_is_exact_single_use_and_credential_bound(
    database_url: str,
    repositories: tuple[
        PostgresAdmissionRepository,
        PostgresExecutionRepository,
        PostgresRoutingRepository,
        PostgresBudgetRepository,
    ],
) -> None:
    admission, _execution, routing, _budget = repositories
    now = _now()
    with pytest.raises(RoutingError) as malformed:
        routing.create_diagnostic_grant(
            _grant_context(now),
            exact_route_id="not-a-route-id",
            reason="diagnostic test",
            now=now,
        )
    assert malformed.value.code is RoutingErrorCode.NOT_FOUND

    grant = routing.create_diagnostic_grant(
        _grant_context(now),
        exact_route_id=ROUTE_ID,
        reason="diagnostic test",
        now=now,
    )
    requests = (
        _exact_request(_uuidv7(now, 24), grant.grant),
        _exact_request(_uuidv7(now, 25), grant.grant),
    )

    def consume(request: AdmissionRequest) -> str:
        try:
            admission.admit(_context(), request, now=now)
        except AdmissionError as error:
            return error.code.value
        return "created"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(consume, requests))
    assert sorted(outcomes) == [
        "created",
        AdmissionErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED.value,
    ]
    with psycopg.connect(database_url) as connection:
        durable = connection.execute(
            """SELECT count(*), count(DISTINCT grant_id),
                      count(DISTINCT request_id)
               FROM router.diagnostic_route_authorizations
               WHERE grant_id = %s""",
            (grant.grant_id,),
        ).fetchone()
    assert durable == (1, 1, 1)

    rotated_grant = routing.create_diagnostic_grant(
        _grant_context(now),
        exact_route_id=ROUTE_ID,
        reason="credential binding test",
        now=now,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.encrypted_credentials
               SET generation = generation + 1, current_revision = %s,
                   last_changed_at = transaction_timestamp()
               WHERE id = (
                   SELECT credential_id FROM router.provider_instances
                   WHERE id = (
                       SELECT provider_instance_id
                       FROM router.provider_model_routes WHERE id = %s
                   )
               )""",
            (uuid.uuid4(), ROUTE_ID),
        )
    with pytest.raises(AdmissionError) as rotated:
        admission.admit(
            _context(),
            _exact_request(_uuidv7(now, 26), rotated_grant.grant),
            now=now,
        )
    assert rotated.value.code is AdmissionErrorCode.DIAGNOSTIC_PERMISSION_REQUIRED

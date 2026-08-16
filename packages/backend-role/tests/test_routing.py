"""Provider-neutral routing coordinator and adapter boundary tests."""
# ruff: noqa: ANN401, ARG002, D103, FBT003, FURB157

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from llmrouter_backend.accounting import PriceComponent, UsageComponent, UsageUnit
from llmrouter_backend.authority import RequestContext
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ErrorScope,
    TerminalError,
    TerminalErrorClass,
)
from llmrouter_backend.health import (
    CircuitSettings,
    HealthPermit,
    HealthScope,
    LocalProviderHealth,
)
from llmrouter_backend.routing import (
    AdapterPhase,
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    AttemptPlan,
    AttemptTimeouts,
    BudgetDecision,
    RoutingCoordinator,
    RoutingError,
    RoutingErrorCode,
    SafeFailureEvidence,
)
from llmrouter_backend.routing.coordinator import (
    _bounded_stop,
    _execute_adapter,
    _recover_dispatched,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from llmrouter_backend.routing import (
        AccountingHook,
        BudgetGate,
        CompletionHook,
        EligibilityGate,
    )

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _plan(
    *,
    execution_ms: int = 1_000,
    partial_output: bool = False,
    committed_effect: bool = False,
) -> AttemptPlan:
    return AttemptPlan(
        claim_id="0198b000-0000-7000-8000-000000000001",
        claim_generation=1,
        request_id="0198b000-0000-7000-8000-000000000002",
        request_row_id="0198b000-0000-7000-8000-000000000003",
        service_id="0198b000-0000-7000-8000-000000000004",
        workspace_id="0198b000-0000-7000-8000-000000000005",
        attempt_id="0198b000-0000-7000-8000-000000000006",
        attempt_number=1,
        candidate_ordinal=1,
        assignment_id="0198b000-0000-7000-8000-000000000007",
        assignment_revision="0198b000-0000-7000-8000-000000000008",
        route_snapshot_id="0198b000-0000-7000-8000-000000000009",
        route_snapshot_sha256=b"r" * 32,
        route_configuration_revision="0198b000-0000-7000-8000-000000000010",
        provider_model_route_id="0198b000-0000-7000-8000-000000000011",
        route_generation=1,
        provider_instance_id="0198b000-0000-7000-8000-000000000012",
        provider_instance_generation=1,
        credential_id="0198b000-0000-7000-8000-000000000013",
        credential_generation=1,
        price_version_id="0198b000-0000-7000-8000-000000000014",
        adapter_type="test-adapter",
        endpoint="https://provider.invalid",
        wire_model="test-model",
        capabilities=frozenset({"chat"}),
        candidate_policy={},
        instance_settings={},
        route_settings={},
        typed_prices=(PriceComponent(UsageUnit.REQUEST, Decimal("1"), "USD", "1"),),
        timeouts=AttemptTimeouts(
            min(100, execution_ms),
            min(100, execution_ms),
            min(100, execution_ms),
            execution_ms,
        ),
        logical_deadline=NOW + timedelta(minutes=15),
        attempt_deadline=NOW + timedelta(milliseconds=execution_ms),
        diagnostic=False,
        partial_output=partial_output,
        committed_effect=committed_effect,
        started=False,
        dispatched=False,
        recovery_only=False,
        recovery_failure=None,
        prestart_reservation_id=None,
        request_terminal=False,
    )


class _Adapter:
    def __init__(
        self,
        execute: Callable[[AttemptPlan, Callable[[AdapterPhase], None]], Any],
        *,
        stop: AdapterStopEvidence | None = None,
    ) -> None:
        self._execute = execute
        self._stop = stop
        self.execute_calls = 0
        self.cancel_calls = 0

    def execute(
        self, plan: AttemptPlan, progress: Callable[[AdapterPhase], None]
    ) -> Any:
        self.execute_calls += 1
        return self._execute(plan, progress)

    def cancel(self, plan: AttemptPlan) -> AdapterStopEvidence:
        self.cancel_calls += 1
        if self._stop is None:
            raise RuntimeError
        return self._stop


def _success(
    _plan_value: AttemptPlan, progress: Callable[[AdapterPhase], None]
) -> AdapterResult:
    progress(AdapterPhase.CONNECTED)
    progress(AdapterPhase.FIRST_BYTE)
    return AdapterResult(AttemptOutcome.SUCCEEDED)


def _failure(
    plan: AttemptPlan,
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    *,
    outcome: AttemptOutcome = AttemptOutcome.FAILED,
    detail: str = "test_failure",
) -> AdapterResult:
    scope_id = {
        ErrorScope.ATTEMPT: plan.attempt_id,
        ErrorScope.PROVIDER_MODEL_ROUTE: plan.provider_model_route_id,
        ErrorScope.PROVIDER_INSTANCE: plan.provider_instance_id,
        ErrorScope.CREDENTIAL: plan.credential_id,
        ErrorScope.ASSIGNMENT_CANDIDATE: (
            f"{plan.assignment_id}:{plan.candidate_ordinal}"
        ),
        ErrorScope.LOGICAL_REQUEST: plan.request_id,
    }[scope]
    return AdapterResult(
        outcome,
        AttemptFailure(
            TerminalError(error_class, scope, "The provider attempt failed."),
            scope_id,
            SafeFailureEvidence(detail_code=detail),
        ),
    )


def _delayed_adapter(
    before_delay: tuple[AdapterPhase, ...],
    after_delay: tuple[AdapterPhase, ...],
) -> Callable[[AttemptPlan, Callable[[AdapterPhase], None]], AdapterResult]:
    def execute(
        _plan_value: AttemptPlan, progress: Callable[[AdapterPhase], None]
    ) -> AdapterResult:
        for phase in before_delay:
            progress(phase)
        time.sleep(0.12)
        for phase in after_delay:
            progress(phase)
        return AdapterResult(AttemptOutcome.SUCCEEDED)

    return execute


@pytest.mark.parametrize("safe_code", ["", "bad\ncode", "bad\x00code"])
def test_stop_evidence_rejects_unsafe_codes_at_routing_boundary(
    safe_code: str,
) -> None:
    plan = _plan()
    adapter = _Adapter(
        _success,
        stop=AdapterStopEvidence(plan.attempt_id, True, True, True, safe_code),
    )

    assert _bounded_stop(adapter, plan) is None


def test_stop_evidence_must_match_the_exact_attempt() -> None:
    plan = _plan()
    adapter = _Adapter(
        _success,
        stop=AdapterStopEvidence("another-operation", True, True, True),
    )

    assert _bounded_stop(adapter, plan) is None


@pytest.mark.parametrize(
    ("execute", "detail"),
    [
        (
            _delayed_adapter((), (AdapterPhase.CONNECTED, AdapterPhase.FIRST_BYTE)),
            "connect_timeout",
        ),
        (
            _delayed_adapter((AdapterPhase.CONNECTED,), (AdapterPhase.FIRST_BYTE,)),
            "first_byte_timeout",
        ),
        (
            _delayed_adapter((AdapterPhase.CONNECTED, AdapterPhase.FIRST_BYTE), ()),
            "idle_timeout",
        ),
    ],
)
def test_adapter_phase_timeouts_are_enforced(
    execute: Callable[[AttemptPlan, Callable[[AdapterPhase], None]], AdapterResult],
    detail: str,
) -> None:
    plan = _plan()
    adapter = _Adapter(
        execute,
        stop=AdapterStopEvidence(plan.attempt_id, True, True, True),
    )

    result = _execute_adapter(adapter, plan, now=NOW)

    assert result.outcome is AttemptOutcome.FAILED
    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.TIMEOUT
    assert result.failure.evidence.detail_code == detail


def test_invalid_adapter_result_is_a_request_wide_router_failure() -> None:
    def invalid(
        _plan_value: AttemptPlan, progress: Callable[[AdapterPhase], None]
    ) -> object:
        progress(AdapterPhase.CONNECTED)
        progress(AdapterPhase.FIRST_BYTE)
        return object()

    result = _execute_adapter(_Adapter(invalid), _plan(), now=NOW)

    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.ROUTER_INTERNAL
    assert result.failure.error.affected_scope is ErrorScope.LOGICAL_REQUEST
    assert result.failure.evidence.detail_code == "invalid_adapter_result"


def test_dispatched_recovery_falls_back_only_after_exact_confirmed_stop() -> None:
    plan = _plan()
    confirmed = _Adapter(
        _success,
        stop=AdapterStopEvidence(plan.attempt_id, True, True, True),
    )
    unproved = _Adapter(
        _success,
        stop=AdapterStopEvidence("other", True, True, True),
    )

    safe = _recover_dispatched(confirmed, plan)
    uncertain = _recover_dispatched(unproved, plan)

    assert safe.outcome is AttemptOutcome.FAILED
    assert safe.failure is not None
    assert safe.failure.error.affected_scope is ErrorScope.ATTEMPT
    assert uncertain.outcome is AttemptOutcome.UNCERTAIN
    assert uncertain.failure is not None
    assert uncertain.failure.error.affected_scope is ErrorScope.LOGICAL_REQUEST
    assert confirmed.execute_calls == unproved.execute_calls == 0


def test_dispatched_recovery_keeps_a_visible_commit_boundary() -> None:
    plan = _plan(partial_output=True)
    adapter = _Adapter(
        _success,
        stop=AdapterStopEvidence(plan.attempt_id, True, True, True),
    )

    result = _recover_dispatched(adapter, plan)

    assert result.outcome is AttemptOutcome.INTERRUPTED
    assert result.failure is not None
    assert result.failure.error.affected_scope is ErrorScope.LOGICAL_REQUEST


def _coordinator(  # noqa: PLR0913
    repository: MagicMock,
    *,
    eligibility: Callable[[AttemptPlan], AttemptFailure | None],
    budget: MagicMock,
    adapter: Callable[[str], _Adapter],
    accounting: Callable[[AttemptPlan, AdapterResult], None] = lambda _p, _r: None,
    completion: Callable[[AttemptPlan, AdapterResult], None] = lambda _p, _r: None,
    health: LocalProviderHealth | None = None,
) -> RoutingCoordinator:
    repository.pending_accounting.return_value = None
    return RoutingCoordinator(
        repository,
        eligibility=cast("EligibilityGate", eligibility),
        budget=cast("BudgetGate", budget),
        adapter=adapter,
        accounting=cast("AccountingHook", accounting),
        completion=cast("CompletionHook", completion),
        clock=lambda: NOW,
        health=health,
    )


def _context() -> RequestContext:
    return cast("RequestContext", object())


def _health_context() -> RequestContext:
    context = MagicMock(spec=RequestContext)
    context.operation = "model.create"
    return cast("RequestContext", context)


def _open_health(
    plan: AttemptPlan,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> LocalProviderHealth:
    health = LocalProviderHealth(
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            open_duration=timedelta(seconds=1),
            jitter_ratio=0,
        ),
        clock=clock,
        jitter=lambda _bound: 0,
    )
    permit = health.acquire_plan(plan, operation="model.create")
    health.record_plan_result(
        plan,
        _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT),
        permit,
        operation="model.create",
    )
    return health


def test_open_health_circuit_suppresses_before_budget_and_falls_back() -> None:
    plan = _plan()
    health = _open_health(plan)
    repository = MagicMock()
    repository.claim.side_effect = [
        plan,
        RoutingError(RoutingErrorCode.NO_CANDIDATE, plan.request_id),
    ]
    budget = MagicMock()
    completed: list[AdapterResult] = []
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        completion=lambda _plan_value, result: completed.append(result),
        health=health,
    )

    result = coordinator.execute(
        _health_context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.PROVIDER_UNAVAILABLE
    assert result.failure.error.affected_scope is ErrorScope.PROVIDER_MODEL_ROUTE
    assert result.failure.evidence.detail_code == "local_circuit_open"
    assert completed == [result]
    budget.reserve.assert_not_called()


def test_missing_health_decision_fails_before_budget_or_dispatch() -> None:
    plan = _plan()
    health = MagicMock(spec=LocalProviderHealth)
    health.acquire_plan.return_value = None
    repository = MagicMock()
    repository.claim.return_value = plan
    budget = MagicMock()
    adapter = _Adapter(_success)
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: adapter,
        health=cast("LocalProviderHealth", health),
    )

    result = coordinator.execute(
        _health_context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.failure is not None
    assert result.failure.error.error_class is TerminalErrorClass.ROUTER_INTERNAL
    assert result.failure.evidence.detail_code == "health_gate_failed"
    budget.reserve.assert_not_called()
    repository.dispatch.assert_not_called()
    assert adapter.execute_calls == 0


def test_health_component_snapshot_records_provider_result_without_replacement() -> (
    None
):
    plan = _plan()
    first_health = MagicMock(spec=LocalProviderHealth)
    replacement_health = MagicMock(spec=LocalProviderHealth)
    permit = HealthPermit(
        allowed=True,
        scope=HealthScope(
            plan.provider_instance_id,
            plan.provider_model_route_id,
            plan.route_generation,
            "model.create",
        ),
    )
    repository = MagicMock()
    repository.claim.return_value = plan
    repository.dispatch.return_value = True
    repository.finish.side_effect = lambda _plan, result, *_args, **_kwargs: result
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    adapter = _Adapter(_success)
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: adapter,
        health=cast("LocalProviderHealth", first_health),
    )

    def acquire(*_args: object, **_kwargs: object) -> HealthPermit:
        coordinator._health = cast(  # noqa: SLF001
            "LocalProviderHealth", replacement_health
        )
        return permit

    first_health.acquire_plan.side_effect = acquire

    result = coordinator.execute(
        _health_context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.outcome is AttemptOutcome.SUCCEEDED
    first_health.record_plan_result.assert_called_once_with(
        plan, result, permit, operation="model.create"
    )
    replacement_health.record_plan_result.assert_not_called()


def test_half_open_probe_uses_policy_budget_and_accounting_controls() -> None:
    plan = _plan()
    current = [NOW]
    health = _open_health(plan, clock=lambda: current[0])
    current[0] += timedelta(seconds=1)
    repository = MagicMock()
    repository.claim.return_value = plan
    repository.dispatch.return_value = True
    repository.finish.side_effect = lambda _plan, result, *_args, **_kwargs: result
    eligibility = MagicMock(return_value=None)
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    accounting = MagicMock()
    adapter = _Adapter(_success)
    coordinator = _coordinator(
        repository,
        eligibility=eligibility,
        budget=budget,
        adapter=lambda _kind: adapter,
        accounting=accounting,
        health=health,
    )

    result = coordinator.execute(
        _health_context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.outcome is AttemptOutcome.SUCCEEDED
    eligibility.assert_called_once_with(plan)
    budget.reserve.assert_called_once_with(plan)
    repository.start.assert_called_once_with(plan, budget_reservation_id="reservation")
    accounting.assert_called_once_with(plan, result)
    assert adapter.execute_calls == 1
    assert health.inspect()[0].local_state.value == "closed"


def test_last_prestart_candidate_failure_is_preserved() -> None:
    plan = _plan()
    failure = _failure(
        plan, TerminalErrorClass.AUTHENTICATION, ErrorScope.CREDENTIAL
    ).failure
    assert failure is not None
    repository = MagicMock()
    repository.claim.side_effect = [
        plan,
        RoutingError(RoutingErrorCode.NO_CANDIDATE, plan.request_id),
    ]
    budget = MagicMock()
    completed: list[AdapterResult] = []
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: failure,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        completion=lambda _plan_value, result: completed.append(result),
    )

    result = coordinator.execute(
        _context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.failure == failure
    assert completed == [result]
    repository.reject_before_start.assert_called_once()
    budget.reserve.assert_not_called()


def test_exhausted_pending_fallback_replays_completion_after_failure() -> None:
    plan = _plan()
    result = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)
    repository = MagicMock()
    budget = MagicMock()
    accounting = MagicMock()
    completion_calls = 0

    def completion(_plan_value: AttemptPlan, _result: AdapterResult) -> None:
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise RuntimeError

    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        accounting=accounting,
        completion=completion,
    )
    repository.pending_accounting.return_value = (plan, result, True)
    repository.claim.side_effect = RoutingError(
        RoutingErrorCode.NO_CANDIDATE, plan.request_id
    )

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
    assert error.value.code is RoutingErrorCode.BUSY

    assert (
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
        == result
    )
    assert completion_calls == 2  # noqa: PLR2004
    accounting.assert_not_called()


def test_terminal_prestart_completion_failure_replays_from_durable_decision() -> None:
    plan = _plan()
    result = _failure(plan, TerminalErrorClass.POLICY, ErrorScope.LOGICAL_REQUEST)
    assert result.failure is not None
    repository = MagicMock()
    repository.pending_accounting.side_effect = [None, (plan, result, True)]
    repository.claim.return_value = plan
    completion_calls = 0

    def completion(_plan_value: AttemptPlan, _result: AdapterResult) -> None:
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise RuntimeError

    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: result.failure,
        budget=MagicMock(),
        adapter=lambda _kind: _Adapter(_success),
        completion=completion,
    )
    repository.pending_accounting.side_effect = [None, (plan, result, True)]

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
    assert error.value.code is RoutingErrorCode.BUSY

    assert (
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
        == result
    )
    repository.reject_before_start.assert_called_once()
    assert completion_calls == 2  # noqa: PLR2004


def test_request_terminal_plan_completes_without_a_candidate_rejection() -> None:
    base = _plan()
    result = _failure(
        base,
        TerminalErrorClass.TIMEOUT,
        ErrorScope.LOGICAL_REQUEST,
        detail="logical_deadline",
    )
    assert result.failure is not None
    plan = replace(
        base,
        recovery_only=True,
        recovery_failure=result.failure,
        request_terminal=True,
    )
    repository = MagicMock()
    repository.claim.return_value = plan
    completed: list[AdapterResult] = []
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=MagicMock(),
        adapter=lambda _kind: _Adapter(_success),
        completion=lambda _plan_value, terminal: completed.append(terminal),
    )

    assert (
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
        == result
    )
    repository.reject_before_start.assert_not_called()
    assert completed == [result]


def test_no_start_proof_releases_before_it_removes_the_claim() -> None:
    plan = _plan()
    events: list[str] = []
    repository = MagicMock()
    repository.claim.return_value = plan
    repository.start.side_effect = RuntimeError
    repository.started.return_value = False
    repository.reject_before_start.side_effect = lambda *_args, **_kwargs: (
        events.append("reject")
    )
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    budget.release.side_effect = lambda _reservation: events.append("release")
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
    )

    result = coordinator.execute(
        _context(), request_id=plan.request_id, owner_id="worker"
    )

    assert result.failure is not None
    assert events == ["release", "reject"]


def test_unknown_start_and_accounting_states_return_busy() -> None:
    plan = _plan()
    repository = MagicMock()
    repository.claim.return_value = plan
    repository.start.side_effect = RuntimeError
    repository.started.side_effect = RuntimeError
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
    )

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
    assert error.value.code is RoutingErrorCode.BUSY
    budget.release.assert_not_called()
    repository.reject_before_start.assert_not_called()


def test_adapter_lookup_precedes_dispatch_and_terminal_race_uses_durable_result() -> (
    None
):
    plan = _plan()
    events: list[str] = []
    usage = (UsageComponent(UsageUnit.OUTPUT_TOKEN, Decimal("2")),)

    def provider(
        _plan_value: AttemptPlan, progress: Callable[[AdapterPhase], None]
    ) -> AdapterResult:
        progress(AdapterPhase.CONNECTED)
        progress(AdapterPhase.FIRST_BYTE)
        return AdapterResult(AttemptOutcome.SUCCEEDED, usage=usage)

    adapter = _Adapter(provider)
    cancelled = _failure(
        plan,
        TerminalErrorClass.CANCELLED,
        ErrorScope.LOGICAL_REQUEST,
        outcome=AttemptOutcome.CANCELLED,
        detail="cancel_confirmed",
    )
    repository = MagicMock()
    repository.claim.return_value = plan

    def dispatch(*_args: object, **_kwargs: object) -> bool:
        events.append("dispatch")
        return True

    repository.dispatch.side_effect = dispatch
    repository.finish.return_value = cancelled
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    accounted: list[AdapterResult] = []

    def lookup(_kind: str) -> _Adapter:
        events.append("lookup")
        return adapter

    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lookup,
        accounting=lambda _plan_value, result: accounted.append(result),
    )

    result = coordinator.execute(
        _context(), request_id=plan.request_id, owner_id="worker"
    )

    assert events[:2] == ["lookup", "dispatch"]
    assert result.outcome is AttemptOutcome.CANCELLED
    assert result.usage == usage
    assert accounted == [result]


def test_accounting_failure_keeps_the_durable_result_and_returns_busy() -> None:
    plan = _plan()
    repository = MagicMock()
    repository.claim.return_value = plan
    repository.dispatch.return_value = True
    repository.finish.side_effect = lambda _plan_value, result, *_args, **_kwargs: (
        result
    )
    budget = MagicMock()
    budget.reserve.return_value = BudgetDecision(True, "reservation")
    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        accounting=lambda _plan_value, _result: (_ for _ in ()).throw(RuntimeError()),
    )

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")

    assert error.value.code is RoutingErrorCode.BUSY
    repository.finish.assert_called_once()


def test_pending_terminal_result_retries_accounting_before_completion() -> None:
    plan = _plan()
    result = AdapterResult(AttemptOutcome.SUCCEEDED)
    repository = MagicMock()
    repository.pending_accounting.return_value = (plan, result, False)
    budget = MagicMock()
    accounting_calls = 0
    completed: list[AdapterResult] = []

    def accounting(_plan_value: AttemptPlan, _result: AdapterResult) -> None:
        nonlocal accounting_calls
        accounting_calls += 1
        if accounting_calls == 1:
            raise RuntimeError

    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        accounting=accounting,
        completion=lambda _plan_value, durable: completed.append(durable),
    )
    repository.pending_accounting.return_value = (plan, result, False)

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
    assert error.value.code is RoutingErrorCode.BUSY

    assert (
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
        == result
    )
    assert accounting_calls == 2  # noqa: PLR2004
    assert completed == [result]
    repository.claim.assert_not_called()


def test_accounted_terminal_result_replays_completion_and_returns_result() -> None:
    plan = _plan()
    result = AdapterResult(AttemptOutcome.SUCCEEDED)
    repository = MagicMock()
    repository.pending_accounting.side_effect = [
        (plan, result, False),
        (plan, result, True),
    ]
    budget = MagicMock()
    accounted: list[AdapterResult] = []
    completion_calls = 0

    def completion(_plan_value: AttemptPlan, _result: AdapterResult) -> None:
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise RuntimeError

    coordinator = _coordinator(
        repository,
        eligibility=lambda _plan_value: None,
        budget=budget,
        adapter=lambda _kind: _Adapter(_success),
        accounting=lambda _plan_value, durable: accounted.append(durable),
        completion=completion,
    )

    with pytest.raises(RoutingError) as error:
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
    assert error.value.code is RoutingErrorCode.BUSY

    assert (
        coordinator.execute(_context(), request_id=plan.request_id, owner_id="worker")
        == result
    )
    assert accounted == [result]
    assert completion_calls == 2  # noqa: PLR2004
    repository.claim.assert_not_called()

"""Provider-neutral routing and fallback coordination."""
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915, S101, TRY300

from __future__ import annotations

import concurrent.futures
import queue
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ErrorScope,
    TerminalError,
    TerminalErrorClass,
)

from .errors import RoutingError, RoutingErrorCode
from .model import (
    AdapterPhase,
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    FallbackDecision,
    SafeFailureEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from llmrouter_backend.authority import RequestContext
    from llmrouter_backend.health import HealthPermit, LocalProviderHealth

    from .model import (
        AccountingHook,
        AttemptPlan,
        BudgetGate,
        CompletionHook,
        EligibilityGate,
        ProviderAdapter,
    )
    from .repository import PostgresRoutingRepository


class RoutingCoordinator:
    """Run eligible candidates until one succeeds or a safe stop applies."""

    def __init__(
        self,
        repository: PostgresRoutingRepository,
        *,
        eligibility: EligibilityGate,
        budget: BudgetGate,
        adapter: Callable[[str], ProviderAdapter],
        accounting: AccountingHook,
        completion: CompletionHook,
        clock: Callable[[], datetime],
        health: LocalProviderHealth | None = None,
    ) -> None:
        """Use explicit controls and adapter lookup for each immutable plan."""
        self._repository = repository
        self._eligibility = eligibility
        self._budget = budget
        self._adapter = adapter
        self._accounting = accounting
        self._completion = completion
        self._clock = clock
        self._health = health

    def execute(
        self, context: RequestContext, *, request_id: str, owner_id: str
    ) -> AdapterResult:
        """Return the terminal provider result after bounded durable fallback."""
        last_plan: AttemptPlan | None = None
        last_failure: AdapterResult | None = None
        pending = self._repository.pending_accounting(context, request_id=request_id)
        if pending is not None:
            pending_plan, pending_result, accounting_complete = pending
            if not accounting_complete:
                try:
                    self._accounting(pending_plan, pending_result)
                except Exception as error:
                    raise RoutingError(RoutingErrorCode.BUSY, request_id) from error
            if _decision(pending_result) is not FallbackDecision.NEXT_CANDIDATE:
                try:
                    self._completion(pending_plan, pending_result)
                except Exception as error:
                    raise RoutingError(RoutingErrorCode.BUSY, request_id) from error
                return pending_result
            last_plan = pending_plan
            last_failure = pending_result
        while True:
            try:
                plan = self._repository.claim(
                    context, request_id=request_id, owner_id=owner_id
                )
            except RoutingError as error:
                if (
                    error.code is RoutingErrorCode.NO_CANDIDATE
                    and last_failure is not None
                ):
                    assert last_plan is not None
                    try:
                        self._completion(last_plan, last_failure)
                    except Exception as completion_error:
                        raise RoutingError(
                            RoutingErrorCode.BUSY, request_id
                        ) from completion_error
                    return last_failure
                raise
            if plan.recovery_only:
                if not plan.started:
                    recovery_result = _recovery_failure_result(plan)
                    assert recovery_result.failure is not None
                    recovery_decision = _decision(recovery_result)
                    if plan.prestart_reservation_id is not None:
                        try:
                            self._budget.release(plan.prestart_reservation_id)
                        except Exception as error:
                            raise RoutingError(
                                RoutingErrorCode.BUSY, plan.request_id
                            ) from error
                    self._complete_before_start(
                        plan, recovery_result, recovery_decision
                    )
                    if recovery_decision is FallbackDecision.NEXT_CANDIDATE:
                        last_plan = plan
                        last_failure = recovery_result
                        continue
                    return recovery_result
                if plan.dispatched:
                    try:
                        recovery_adapter = self._adapter(plan.adapter_type)
                    except Exception:  # noqa: BLE001
                        recovery_result = _safe_result(
                            plan,
                            TerminalErrorClass.UNCERTAIN_EFFECT,
                            ErrorScope.LOGICAL_REQUEST,
                            "recovery_adapter_unavailable",
                            outcome=AttemptOutcome.UNCERTAIN,
                        )
                    else:
                        recovery_result = _recover_dispatched(recovery_adapter, plan)
                else:
                    recovery_result = _recovery_failure_result(plan)
                completed, completed_decision = self._complete(plan, recovery_result)
                if completed_decision is FallbackDecision.NEXT_CANDIDATE:
                    last_plan = plan
                    last_failure = completed
                    continue
                return completed
            try:
                failure = self._eligibility(plan)
            except Exception:  # noqa: BLE001
                failure = _safe_result(
                    plan,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "eligibility_gate_failed",
                ).failure
            if failure is not None:
                decision = _fallback(
                    AttemptOutcome.FAILED,
                    failure.error.error_class,
                    failure.error.affected_scope,
                )
                failure_result = AdapterResult(AttemptOutcome.FAILED, failure)
                self._complete_before_start(plan, failure_result, decision)
                if decision is FallbackDecision.NEXT_CANDIDATE:
                    last_plan = plan
                    last_failure = failure_result
                    continue
                return failure_result
            health_decision: tuple[LocalProviderHealth, HealthPermit] | None = None
            health = self._health
            if health is not None:
                try:
                    health_permit = health.acquire_plan(
                        plan, operation=context.operation
                    )
                except Exception:  # noqa: BLE001
                    failure_result = _safe_result(
                        plan,
                        TerminalErrorClass.ROUTER_INTERNAL,
                        ErrorScope.LOGICAL_REQUEST,
                        "health_gate_failed",
                    )
                    self._complete_before_start(
                        plan, failure_result, FallbackDecision.STOP_REQUEST
                    )
                    return failure_result
                if health_permit is None:
                    failure_result = _safe_result(
                        plan,
                        TerminalErrorClass.ROUTER_INTERNAL,
                        ErrorScope.LOGICAL_REQUEST,
                        "health_gate_failed",
                    )
                    self._complete_before_start(
                        plan, failure_result, FallbackDecision.STOP_REQUEST
                    )
                    return failure_result
                if not health_permit.allowed:
                    failure_result = _safe_result(
                        plan,
                        TerminalErrorClass.PROVIDER_UNAVAILABLE,
                        ErrorScope.PROVIDER_MODEL_ROUTE,
                        health_permit.reason or "health_circuit_open",
                    )
                    self._complete_before_start(
                        plan, failure_result, FallbackDecision.NEXT_CANDIDATE
                    )
                    last_plan = plan
                    last_failure = failure_result
                    continue
                health_decision = (health, health_permit)
            try:
                budget = self._budget.reserve(plan)
            except Exception:  # noqa: BLE001
                self._abandon_health(health_decision)
                failure_result = _safe_result(
                    plan,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "budget_gate_failed",
                )
                assert failure_result.failure is not None
                self._complete_before_start(
                    plan, failure_result, FallbackDecision.STOP_REQUEST
                )
                return failure_result
            if not budget.permitted:
                self._abandon_health(health_decision)
                assert budget.failure is not None
                decision = _fallback(
                    AttemptOutcome.FAILED,
                    budget.failure.error.error_class,
                    budget.failure.error.affected_scope,
                )
                failure_result = AdapterResult(AttemptOutcome.FAILED, budget.failure)
                self._complete_before_start(plan, failure_result, decision)
                if decision is FallbackDecision.NEXT_CANDIDATE:
                    last_plan = plan
                    last_failure = failure_result
                    continue
                return failure_result
            assert budget.reservation_id is not None
            try:
                self._repository.start(
                    plan, budget_reservation_id=budget.reservation_id
                )
            except Exception:  # noqa: BLE001
                try:
                    durable_start = self._repository.started(
                        plan, budget_reservation_id=budget.reservation_id
                    )
                except Exception:  # noqa: BLE001
                    self._abandon_health(health_decision)
                    raise RoutingError(RoutingErrorCode.BUSY, plan.request_id) from None
                if durable_start:
                    pass
                else:
                    self._abandon_health(health_decision)
                    failure_result = _safe_result(
                        plan,
                        TerminalErrorClass.ROUTER_INTERNAL,
                        ErrorScope.LOGICAL_REQUEST,
                        "start_blocked",
                    )
                    assert failure_result.failure is not None
                    try:
                        self._budget.release(budget.reservation_id)
                    except Exception as error:
                        raise RoutingError(
                            RoutingErrorCode.BUSY, plan.request_id
                        ) from error
                    self._complete_before_start(
                        plan, failure_result, FallbackDecision.STOP_REQUEST
                    )
                    return failure_result
            try:
                adapter = self._adapter(plan.adapter_type)
            except Exception:  # noqa: BLE001
                self._abandon_health(health_decision)
                failure_result = _safe_result(
                    plan,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "adapter_lookup_failed",
                )
                result = failure_result
            else:
                dispatched: bool | None = None
                try:
                    dispatched = self._repository.dispatch(plan, owner_id=owner_id)
                except Exception:  # noqa: BLE001
                    with suppress(Exception):
                        dispatched = self._repository.dispatch(plan, owner_id=owner_id)
                if dispatched is None:
                    self._abandon_health(health_decision)
                    result = _safe_result(
                        plan,
                        TerminalErrorClass.ROUTER_INTERNAL,
                        ErrorScope.LOGICAL_REQUEST,
                        "dispatch_blocked",
                    )
                else:
                    try:
                        result = (
                            _execute_adapter(adapter, plan, now=self._clock())
                            if dispatched
                            else _recover_dispatched(adapter, plan)
                        )
                        if dispatched and health_decision is not None:
                            result_health, result_permit = health_decision
                            with suppress(Exception):
                                result_health.record_plan_result(
                                    plan,
                                    result,
                                    result_permit,
                                    operation=context.operation,
                                )
                    finally:
                        self._abandon_health(health_decision)
            accounting_result, durable_decision = self._complete(plan, result)
            if durable_decision is not FallbackDecision.NEXT_CANDIDATE:
                return accounting_result
            last_plan = plan
            last_failure = accounting_result

    def _complete(
        self, plan: AttemptPlan, result: AdapterResult
    ) -> tuple[AdapterResult, FallbackDecision]:
        """Finish and account the authoritative durable attempt outcome."""
        durable_result = self._repository.finish(
            plan, result, _decision(result), now=self._clock()
        )
        accounting_result = durable_result
        if durable_result != result and result.usage:
            accounting_result = AdapterResult(
                durable_result.outcome, durable_result.failure, result.usage
            )
        try:
            self._accounting(plan, accounting_result)
        except Exception as error:
            raise RoutingError(RoutingErrorCode.BUSY, plan.request_id) from error
        decision = _decision(durable_result)
        if decision is not FallbackDecision.NEXT_CANDIDATE:
            try:
                self._completion(plan, accounting_result)
            except Exception as error:
                raise RoutingError(RoutingErrorCode.BUSY, plan.request_id) from error
        return accounting_result, decision

    @staticmethod
    def _abandon_health(
        decision: tuple[LocalProviderHealth, HealthPermit] | None,
    ) -> None:
        """Release a half-open slot when no new provider evidence exists."""
        if decision is not None:
            health, permit = decision
            with suppress(Exception):
                health.abandon(permit)

    def _complete_before_start(
        self,
        plan: AttemptPlan,
        result: AdapterResult,
        decision: FallbackDecision,
    ) -> None:
        """Persist a no-start result and complete each non-fallback request."""
        if not plan.request_terminal:
            assert result.failure is not None
            self._repository.reject_before_start(
                plan, result.failure, decision, now=self._clock()
            )
        if decision is not FallbackDecision.NEXT_CANDIDATE:
            try:
                self._completion(plan, result)
            except Exception as error:
                raise RoutingError(RoutingErrorCode.BUSY, plan.request_id) from error


def _fallback(
    outcome: AttemptOutcome, error_class: TerminalErrorClass, scope: ErrorScope
) -> FallbackDecision:
    if outcome is AttemptOutcome.CANCELLED:
        return FallbackDecision.CANCELLED
    if outcome in {AttemptOutcome.INTERRUPTED, AttemptOutcome.UNCERTAIN}:
        return FallbackDecision.COMMIT_BOUNDARY
    if error_class is TerminalErrorClass.CANCELLED:
        return FallbackDecision.CANCELLED
    if error_class is TerminalErrorClass.UNCERTAIN_EFFECT:
        return FallbackDecision.COMMIT_BOUNDARY
    if scope is ErrorScope.LOGICAL_REQUEST:
        return FallbackDecision.STOP_REQUEST
    return FallbackDecision.NEXT_CANDIDATE


def _decision(result: AdapterResult) -> FallbackDecision:
    """Derive the decision from the durable terminal result."""
    if result.outcome is AttemptOutcome.SUCCEEDED:
        return FallbackDecision.SUCCEEDED
    assert result.failure is not None
    return _fallback(
        result.outcome,
        result.failure.error.error_class,
        result.failure.error.affected_scope,
    )


def _execute_adapter(
    adapter: ProviderAdapter, plan: AttemptPlan, *, now: datetime
) -> AdapterResult:
    """Enforce connect, first-byte, idle, and total operation deadlines."""
    completed = object()
    milestones: queue.Queue[AdapterPhase | object] = queue.Queue()

    def report(phase: AdapterPhase) -> None:
        milestones.put(phase)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(adapter.execute, plan, report)
    future.add_done_callback(lambda _future: milestones.put(completed))
    started = time.monotonic()
    total_seconds = max(0.0, (plan.attempt_deadline - now).total_seconds())
    expected = AdapterPhase.CONNECTED
    phase_seconds = plan.timeouts.connect_ms / 1_000
    try:
        while True:
            remaining = total_seconds - (time.monotonic() - started)
            if remaining <= 0:
                return _timeout_result(adapter, plan, "execution_timeout")
            wait_seconds = min(phase_seconds, remaining)
            try:
                milestone = milestones.get(timeout=wait_seconds)
            except queue.Empty:
                detail = (
                    "execution_timeout"
                    if remaining <= phase_seconds
                    else {
                        AdapterPhase.CONNECTED: "connect_timeout",
                        AdapterPhase.FIRST_BYTE: "first_byte_timeout",
                        AdapterPhase.PROGRESS: "idle_timeout",
                    }[expected]
                )
                return _timeout_result(adapter, plan, detail)
            if milestone is completed:
                try:
                    result = future.result()
                except Exception:  # noqa: BLE001
                    return _safe_result(
                        plan,
                        TerminalErrorClass.TRANSPORT,
                        ErrorScope.ATTEMPT,
                        "adapter_failure",
                    )
                if expected is not AdapterPhase.PROGRESS or not isinstance(
                    result, AdapterResult
                ):
                    return _safe_result(
                        plan,
                        TerminalErrorClass.ROUTER_INTERNAL,
                        ErrorScope.LOGICAL_REQUEST,
                        "invalid_adapter_result",
                    )
                return result
            if milestone is not expected:
                return _safe_result(
                    plan,
                    TerminalErrorClass.ROUTER_INTERNAL,
                    ErrorScope.LOGICAL_REQUEST,
                    "invalid_adapter_progress",
                )
            if expected is AdapterPhase.CONNECTED:
                expected = AdapterPhase.FIRST_BYTE
                phase_seconds = plan.timeouts.first_byte_ms / 1_000
            else:
                expected = AdapterPhase.PROGRESS
                phase_seconds = plan.timeouts.idle_ms / 1_000
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _timeout_result(
    adapter: ProviderAdapter, plan: AttemptPlan, detail_code: str
) -> AdapterResult:
    """Return a timeout only when this exact operation is proved stopped."""
    stop = _bounded_stop(adapter, plan)
    if stop is None:
        return _safe_result(
            plan,
            TerminalErrorClass.UNCERTAIN_EFFECT,
            ErrorScope.LOGICAL_REQUEST,
            "stop_unproved",
            outcome=AttemptOutcome.UNCERTAIN,
        )
    if stop.confirmed_stopped:
        return _safe_result(
            plan, TerminalErrorClass.TIMEOUT, ErrorScope.ATTEMPT, detail_code
        )
    return _safe_result(
        plan,
        TerminalErrorClass.UNCERTAIN_EFFECT,
        ErrorScope.LOGICAL_REQUEST,
        "uncertain_effect",
        outcome=AttemptOutcome.UNCERTAIN,
    )


def _recover_dispatched(adapter: ProviderAdapter, plan: AttemptPlan) -> AdapterResult:
    """Stop and reconcile dispatched work without a duplicate execute call."""
    stop = _bounded_stop(adapter, plan)
    if plan.partial_output or plan.committed_effect:
        if stop is not None and stop.confirmed_stopped:
            return _safe_result(
                plan,
                TerminalErrorClass.TRANSPORT,
                ErrorScope.LOGICAL_REQUEST,
                "recovered_after_commit",
                outcome=AttemptOutcome.INTERRUPTED,
            )
        return _safe_result(
            plan,
            TerminalErrorClass.UNCERTAIN_EFFECT,
            ErrorScope.LOGICAL_REQUEST,
            "committed_recovery_unproved",
            outcome=AttemptOutcome.UNCERTAIN,
        )
    if stop is not None and stop.confirmed_stopped:
        if plan.recovery_failure is not None:
            return _recovery_failure_result(plan)
        return _safe_result(
            plan,
            TerminalErrorClass.TRANSPORT,
            ErrorScope.ATTEMPT,
            "recovered_dispatch_stopped",
        )
    return _safe_result(
        plan,
        TerminalErrorClass.UNCERTAIN_EFFECT,
        ErrorScope.LOGICAL_REQUEST,
        "dispatched_recovery",
        outcome=AttemptOutcome.UNCERTAIN,
    )


def _recovery_failure_result(plan: AttemptPlan) -> AdapterResult:
    """Use the durable recovery cause without widening its affected scope."""
    failure = plan.recovery_failure
    if failure is None:
        return _safe_result(
            plan,
            TerminalErrorClass.TRANSPORT,
            ErrorScope.ATTEMPT,
            "recovered_before_dispatch",
        )
    outcome = (
        AttemptOutcome.CANCELLED
        if failure.error.error_class is TerminalErrorClass.CANCELLED
        else AttemptOutcome.FAILED
    )
    return AdapterResult(outcome, failure)


def _bounded_stop(
    adapter: ProviderAdapter, plan: AttemptPlan
) -> AdapterStopEvidence | None:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(adapter.cancel, plan)
    try:
        result = future.result(timeout=min(5.0, plan.timeouts.connect_ms / 1_000))
        if not isinstance(result, AdapterStopEvidence):
            return None
        if result.operation_id != plan.attempt_id:
            return None
        if result.safe_code is not None and (
            not result.safe_code
            or any(not " " <= character <= "~" for character in result.safe_code)
        ):
            return None
        return result
    except Exception:  # noqa: BLE001
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _safe_result(
    plan: AttemptPlan,
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    detail_code: str,
    *,
    outcome: AttemptOutcome = AttemptOutcome.FAILED,
) -> AdapterResult:
    error = TerminalError(error_class, scope, "The provider attempt did not complete.")
    affected_scope_id = (
        plan.request_id if scope is ErrorScope.LOGICAL_REQUEST else plan.attempt_id
    )
    failure = AttemptFailure(
        error,
        affected_scope_id,
        SafeFailureEvidence(detail_code=detail_code),
    )
    return AdapterResult(outcome, failure)

"""Local provider health circuit tests."""
# ruff: noqa: D103, PLR2004

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from llmrouter_backend.execution import ErrorScope, TerminalError, TerminalErrorClass
from llmrouter_backend.health import (
    CircuitKey,
    CircuitSettings,
    CircuitState,
    FleetHint,
    FleetHintVerifier,
    LocalProviderHealth,
    ProviderFailureClass,
)
from llmrouter_backend.routing import (
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    SafeFailureEvidence,
)

if TYPE_CHECKING:
    from llmrouter_backend.routing import AttemptPlan

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, value: timedelta) -> None:
        self.now += value


def _plan(  # noqa: PLR0913
    *,
    instance: str = "instance-a",
    route: str = "route-a",
    generation: int = 1,
    instance_generation: int = 1,
    credential: str = "credential-a",
    credential_generation: int = 1,
    required_capabilities: frozenset[str] = frozenset(),
) -> AttemptPlan:
    return cast(
        "AttemptPlan",
        SimpleNamespace(
            provider_instance_id=instance,
            provider_instance_generation=instance_generation,
            provider_model_route_id=route,
            route_generation=generation,
            attempt_id="attempt-a",
            credential_id=credential,
            credential_generation=credential_generation,
            candidate_policy={
                "required_capabilities": tuple(sorted(required_capabilities))
            },
        ),
    )


def _failure(
    plan: AttemptPlan,
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    *,
    affected_scope_id: str | None = None,
) -> AdapterResult:
    exact_scope = {
        ErrorScope.ATTEMPT: plan.attempt_id,
        ErrorScope.PROVIDER_MODEL_ROUTE: plan.provider_model_route_id,
        ErrorScope.PROVIDER_INSTANCE: plan.provider_instance_id,
        ErrorScope.CREDENTIAL: plan.credential_id,
        ErrorScope.LOGICAL_REQUEST: "request-a",
        ErrorScope.ASSIGNMENT_CANDIDATE: "candidate-a",
    }[scope]
    return AdapterResult(
        AttemptOutcome.FAILED,
        AttemptFailure(
            TerminalError(error_class, scope, "The attempt failed."),
            affected_scope_id or exact_scope,
            SafeFailureEvidence(detail_code="test_failure"),
        ),
    )


def _health(
    clock: _Clock,
    *,
    settings: CircuitSettings | None = None,
    verifier: FleetHintVerifier | None = None,
) -> LocalProviderHealth:
    return LocalProviderHealth(
        settings=settings or CircuitSettings(),
        clock=clock,
        jitter=lambda bound: bound,
        hint_verifier=verifier,
    )


def _record(
    health: LocalProviderHealth,
    plan: AttemptPlan,
    result: AdapterResult,
    *,
    operation: str = "model.create",
) -> None:
    permit = health.acquire_plan(plan, operation=operation)
    assert permit.allowed
    health.record_plan_result(plan, result, permit, operation=operation)


@pytest.mark.parametrize(
    "settings",
    [
        CircuitSettings(window_size=100, minimum_samples=100),
        CircuitSettings(failure_threshold=1),
        CircuitSettings(open_duration=timedelta(minutes=5)),
        CircuitSettings(probe_limit=10),
        CircuitSettings(maximum_backoff=timedelta(minutes=15)),
        CircuitSettings(jitter_ratio=0.5),
    ],
)
def test_settings_accept_global_safety_edges(settings: CircuitSettings) -> None:
    assert settings.window_size <= 100


@pytest.mark.parametrize(
    "values",
    [
        {"window_size": 101},
        {"minimum_samples": 11},
        {"failure_threshold": 0},
        {"open_duration": timedelta(seconds=0)},
        {"probe_limit": 11},
        {"maximum_backoff": timedelta(minutes=16)},
        {"jitter_ratio": 0.51},
    ],
)
def test_settings_reject_values_outside_global_safety_limits(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"outside|must fit"):
        CircuitSettings(**values)  # type: ignore[arg-type]


def test_failures_open_only_the_exact_route_operation_and_class() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=2,
            minimum_samples=2,
            failure_threshold=1,
            jitter_ratio=0,
        ),
    )
    plan = _plan()

    result = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)
    _record(health, plan, result)
    _record(health, plan, result)

    matching = health.acquire_plan(plan, operation="model.create")
    other_route = health.acquire_plan(_plan(route="route-b"), operation="model.create")
    other_operation = health.acquire_plan(plan, operation="embedding.create")
    snapshot = health.inspect()[0]

    assert not matching.allowed
    assert other_route.allowed
    assert other_operation.allowed
    assert snapshot.key.failure_class is ProviderFailureClass.TRANSPORT
    assert snapshot.local_state is CircuitState.OPEN
    assert snapshot.sample_count == snapshot.failure_count == 2


def test_capability_specific_health_does_not_suppress_an_unrelated_capability() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            jitter_ratio=0,
        ),
    )
    vision = _plan(required_capabilities=frozenset({"vision"}))
    text = _plan(required_capabilities=frozenset({"chat"}))

    _record(
        health,
        vision,
        _failure(vision, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT),
    )

    assert not health.acquire_plan(vision, operation="model.create").allowed
    assert health.acquire_plan(text, operation="model.create").allowed


def test_credential_and_instance_revisions_have_independent_health() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            jitter_ratio=0,
        ),
    )
    failed = _plan()
    rotated_credential = _plan(credential_generation=2)
    revised_instance = _plan(instance_generation=2)

    _record(
        health,
        failed,
        _failure(failed, TerminalErrorClass.AUTHENTICATION, ErrorScope.CREDENTIAL),
    )

    assert not health.acquire_plan(failed, operation="model.create").allowed
    assert health.acquire_plan(rotated_credential, operation="model.create").allowed
    assert health.acquire_plan(revised_instance, operation="model.create").allowed


@pytest.mark.parametrize(
    ("error_class", "scope"),
    [
        (TerminalErrorClass.POLICY, ErrorScope.PROVIDER_MODEL_ROUTE),
        (TerminalErrorClass.BUDGET, ErrorScope.LOGICAL_REQUEST),
        (TerminalErrorClass.CANCELLED, ErrorScope.LOGICAL_REQUEST),
        (TerminalErrorClass.INCOMPATIBLE_REQUEST, ErrorScope.ATTEMPT),
        (TerminalErrorClass.ROUTER_INTERNAL, ErrorScope.LOGICAL_REQUEST),
    ],
)
def test_non_provider_results_do_not_change_health(
    error_class: TerminalErrorClass, scope: ErrorScope
) -> None:
    clock = _Clock()
    health = _health(clock)
    plan = _plan()

    _record(health, plan, _failure(plan, error_class, scope))

    assert health.inspect() == ()


def test_mismatched_provider_evidence_does_not_change_health() -> None:
    clock = _Clock()
    health = _health(clock)
    plan = _plan()

    _record(
        health,
        plan,
        _failure(
            plan,
            TerminalErrorClass.TRANSPORT,
            ErrorScope.ATTEMPT,
            affected_scope_id="another-attempt",
        ),
    )

    assert health.inspect() == ()


def test_normal_health_decision_can_record_only_one_result() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=2,
            minimum_samples=2,
            failure_threshold=1,
            jitter_ratio=0,
        ),
    )
    plan = _plan()
    failure = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)
    permit = health.acquire_plan(plan, operation="model.create")

    health.record_plan_result(plan, failure, permit, operation="model.create")
    with pytest.raises(ValueError, match="no longer active"):
        health.record_plan_result(plan, failure, permit, operation="model.create")

    snapshot = health.inspect()[0]
    assert snapshot.sample_count == snapshot.failure_count == 1
    assert snapshot.local_state is CircuitState.CLOSED


def test_abandoned_or_foreign_normal_decision_cannot_record_a_result() -> None:
    clock = _Clock()
    owner = _health(clock)
    foreign = _health(clock)
    plan = _plan()
    failure = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)
    abandoned = owner.acquire_plan(plan, operation="model.create")
    owner.abandon(abandoned)

    with pytest.raises(ValueError, match="no longer active"):
        owner.record_plan_result(plan, failure, abandoned, operation="model.create")
    assert owner.inspect() == ()

    foreign_permit = owner.acquire_plan(plan, operation="model.create")
    with pytest.raises(ValueError, match="no longer active"):
        foreign.record_plan_result(
            plan, failure, foreign_permit, operation="model.create"
        )
    assert foreign.inspect() == ()
    owner.abandon(foreign_permit)


@pytest.mark.parametrize(
    ("error_class", "scope", "normalized"),
    [
        (
            TerminalErrorClass.TIMEOUT,
            ErrorScope.ATTEMPT,
            ProviderFailureClass.TIMEOUT,
        ),
        (
            TerminalErrorClass.TRANSPORT,
            ErrorScope.ATTEMPT,
            ProviderFailureClass.TRANSPORT,
        ),
        (
            TerminalErrorClass.PROVIDER_UNAVAILABLE,
            ErrorScope.PROVIDER_INSTANCE,
            ProviderFailureClass.SERVER,
        ),
        (
            TerminalErrorClass.RATE_LIMIT,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            ProviderFailureClass.RATE_LIMIT,
        ),
        (
            TerminalErrorClass.INVALID_PROVIDER_RESPONSE,
            ErrorScope.PROVIDER_MODEL_ROUTE,
            ProviderFailureClass.INVALID_RESPONSE,
        ),
        (
            TerminalErrorClass.AUTHENTICATION,
            ErrorScope.CREDENTIAL,
            ProviderFailureClass.CREDENTIAL,
        ),
    ],
)
def test_provider_evidence_uses_closed_normalized_failure_classes(
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    normalized: ProviderFailureClass,
) -> None:
    clock = _Clock()
    health = _health(clock)
    plan = _plan()

    _record(health, plan, _failure(plan, error_class, scope))

    snapshot = health.inspect()[0]
    assert snapshot.key.failure_class is normalized
    assert snapshot.sample_count == snapshot.failure_count == 1


def test_new_failure_class_uses_prior_successes_in_its_failure_window() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=5,
            minimum_samples=4,
            failure_threshold=0.6,
            jitter_ratio=0,
        ),
    )
    plan = _plan()
    success = AdapterResult(AttemptOutcome.SUCCEEDED)
    failure = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)

    for _index in range(3):
        _record(health, plan, success)
    assert health.inspect() == ()

    _record(health, plan, failure)
    _record(health, plan, failure)

    snapshot = health.inspect()[0]
    assert snapshot.sample_count == 5
    assert snapshot.failure_count == 2
    assert snapshot.local_state is CircuitState.CLOSED
    assert health.acquire_plan(plan, operation="model.create").allowed


def test_half_open_probe_limit_jitter_backoff_and_success_are_deterministic() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            open_duration=timedelta(seconds=10),
            probe_limit=1,
            maximum_backoff=timedelta(seconds=40),
            jitter_ratio=0.2,
        ),
    )
    plan = _plan()
    result = _failure(plan, TerminalErrorClass.TIMEOUT, ErrorScope.ATTEMPT)
    _record(health, plan, result)
    first_snapshot = health.inspect()[0]

    assert first_snapshot.next_probe_at == NOW + timedelta(seconds=12)
    clock.advance(timedelta(seconds=12))
    probe = health.acquire_plan(plan, operation="model.create")
    blocked = health.acquire_plan(plan, operation="model.create")

    assert probe.allowed
    assert not blocked.allowed
    assert blocked.reason == "half_open_probe_limit"
    health.record_plan_result(
        plan, AdapterResult(AttemptOutcome.SUCCEEDED), probe, operation="model.create"
    )
    snapshot = health.inspect()[0]
    assert snapshot.local_state is CircuitState.CLOSED
    assert snapshot.sample_count == 1
    assert snapshot.failure_count == 0
    assert health.acquire_plan(plan, operation="model.create").allowed


def test_abandoned_probe_releases_concurrency_without_a_health_sample() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            open_duration=timedelta(seconds=1),
            jitter_ratio=0,
        ),
    )
    plan = _plan()
    result = _failure(plan, TerminalErrorClass.RATE_LIMIT, ErrorScope.ATTEMPT)
    _record(health, plan, result)
    clock.advance(timedelta(seconds=1))

    probe = health.acquire_plan(plan, operation="model.create")
    health.abandon(probe)
    replacement = health.acquire_plan(plan, operation="model.create")

    assert probe.allowed
    assert replacement.allowed
    assert health.inspect()[0].sample_count == 1


def test_replayed_probe_abandon_does_not_release_another_probe_slot() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            open_duration=timedelta(seconds=1),
            jitter_ratio=0,
        ),
    )
    plan = _plan()
    _record(
        health,
        plan,
        _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT),
    )
    clock.advance(timedelta(seconds=1))

    first = health.acquire_plan(plan, operation="model.create")
    health.abandon(first)
    replacement = health.acquire_plan(plan, operation="model.create")
    health.abandon(first)
    with pytest.raises(ValueError, match="no longer active"):
        health.record_plan_result(
            plan,
            AdapterResult(AttemptOutcome.SUCCEEDED),
            first,
            operation="model.create",
        )
    blocked = health.acquire_plan(plan, operation="model.create")

    assert replacement.allowed
    assert not blocked.allowed
    assert blocked.reason == "half_open_probe_limit"


def test_backoff_and_jitter_never_exceed_the_configured_maximum() -> None:
    clock = _Clock()
    settings = CircuitSettings(
        window_size=1,
        minimum_samples=1,
        failure_threshold=1,
        open_duration=timedelta(seconds=1),
        maximum_backoff=timedelta(seconds=3),
        jitter_ratio=0.5,
    )
    health = _health(clock, settings=settings)
    plan = _plan()
    failure = _failure(plan, TerminalErrorClass.TRANSPORT, ErrorScope.ATTEMPT)

    for _index in range(20):
        _record(health, plan, failure)
        next_probe_at = health.inspect()[0].next_probe_at
        assert next_probe_at is not None
        assert next_probe_at - clock.now <= settings.maximum_backoff
        clock.now = next_probe_at


def _hint(key: CircuitKey, *, tag: bytes = b"v" * 16) -> FleetHint:
    return FleetHint(
        key,
        NOW - timedelta(seconds=20),
        NOW - timedelta(seconds=10),
        NOW - timedelta(seconds=5),
        NOW + timedelta(seconds=55),
        tag,
    )


def test_only_authentic_fresh_exact_generation_hints_suppress() -> None:
    clock = _Clock()
    health = _health(clock, verifier=lambda hint: hint.authentication_tag == b"v" * 16)
    exact_scope = LocalProviderHealth.scope_for_plan(_plan(), operation="model.create")
    other_generation = LocalProviderHealth.scope_for_plan(
        _plan(generation=2), operation="model.create"
    )
    other_capability = LocalProviderHealth.scope_for_plan(
        _plan(required_capabilities=frozenset({"vision"})),
        operation="model.create",
    )
    key = CircuitKey(exact_scope, ProviderFailureClass.SERVER)

    assert not health.apply_fleet_hint(_hint(key, tag=b"x" * 16))
    assert health.acquire(exact_scope).allowed
    assert health.apply_fleet_hint(_hint(key))
    assert not health.acquire(exact_scope).allowed
    assert health.acquire(other_generation).allowed
    assert health.acquire(other_capability).allowed

    snapshot = health.inspect()[0]
    assert snapshot.local_state is CircuitState.CLOSED
    assert snapshot.fleet_hint_active
    assert not hasattr(snapshot, "authentication_tag")

    clock.advance(timedelta(seconds=56))
    assert health.acquire(exact_scope).allowed
    assert health.inspect() == ()


def test_replayed_or_stale_hint_is_rejected_after_authentication() -> None:
    clock = _Clock()

    def verify(_hint_value: FleetHint) -> bool:
        clock.advance(timedelta(seconds=60))
        return True

    health = _health(clock, verifier=cast("FleetHintVerifier", verify))
    scope = LocalProviderHealth.scope_for_plan(_plan(), operation="model.create")
    hint = _hint(CircuitKey(scope, ProviderFailureClass.SERVER))

    assert not health.apply_fleet_hint(hint)
    assert health.acquire(scope).allowed

    clock.now = NOW
    health = _health(clock, verifier=lambda _hint_value: True)
    assert health.apply_fleet_hint(hint)
    assert not health.apply_fleet_hint(hint)


def test_hint_can_delay_but_cannot_close_a_local_circuit() -> None:
    clock = _Clock()
    health = _health(
        clock,
        settings=CircuitSettings(
            window_size=1,
            minimum_samples=1,
            failure_threshold=1,
            open_duration=timedelta(seconds=10),
            jitter_ratio=0,
        ),
        verifier=lambda _hint_value: True,
    )
    plan = _plan()
    _record(
        health,
        plan,
        _failure(
            plan,
            TerminalErrorClass.PROVIDER_UNAVAILABLE,
            ErrorScope.PROVIDER_MODEL_ROUTE,
        ),
    )
    key = health.inspect()[0].key

    assert health.apply_fleet_hint(_hint(key))
    clock.advance(timedelta(seconds=11))

    denied = health.acquire_plan(plan, operation="model.create")
    assert not denied.allowed
    assert denied.reason == "fleet_hint_open"
    assert health.inspect()[0].local_state is CircuitState.OPEN

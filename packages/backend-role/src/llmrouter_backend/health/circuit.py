"""Thread-safe node-local provider health circuits and fleet hints."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil, log2
from typing import TYPE_CHECKING

from llmrouter_backend.execution import ErrorScope, TerminalErrorClass

from .model import (
    CircuitKey,
    CircuitSettings,
    CircuitSnapshot,
    CircuitState,
    FleetHint,
    FleetHintVerifier,
    HealthPermit,
    HealthScope,
    ProviderFailureClass,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from llmrouter_backend.routing.model import AdapterResult, AttemptPlan


@dataclass(frozen=True, slots=True)
class _Sample:
    succeeded: bool
    occurred_at: datetime


@dataclass(slots=True)
class _Circuit:
    state: CircuitState
    last_state_change: datetime
    samples: deque[_Sample] = field(default_factory=deque)
    next_probe_at: datetime | None = None
    active_probe_tokens: set[object] = field(default_factory=set)
    open_count: int = 0


class LocalProviderHealth:
    """Own local health decisions without a normal-path remote dependency."""

    def __init__(
        self,
        *,
        settings: CircuitSettings | None = None,
        clock: Callable[[], datetime],
        jitter: Callable[[float], float],
        hint_verifier: FleetHintVerifier | None = None,
    ) -> None:
        """Use explicit time, jitter, and trust inputs for deterministic behavior."""
        now = clock()
        _require_aware(now)
        self._settings = settings or CircuitSettings()
        self._clock = clock
        self._jitter = jitter
        self._hint_verifier = hint_verifier
        self._circuits: dict[CircuitKey, _Circuit] = {}
        self._scope_successes: dict[HealthScope, deque[_Sample]] = {}
        self._hints: dict[CircuitKey, FleetHint] = {}
        self._active_decisions: dict[
            object, tuple[HealthScope, tuple[CircuitKey, ...]]
        ] = {}
        self._lock = threading.RLock()

    @property
    def settings(self) -> CircuitSettings:
        """Return the current immutable circuit settings."""
        with self._lock:
            return self._settings

    def update_settings(self, settings: CircuitSettings) -> None:
        """Apply valid bounded settings and trim existing sample windows."""
        if not isinstance(settings, CircuitSettings):
            message = "Health settings must be a CircuitSettings value."
            raise TypeError(message)
        with self._lock:
            self._settings = settings
            for circuit in self._circuits.values():
                circuit.samples = deque(
                    tuple(circuit.samples)[-settings.window_size :],
                    maxlen=settings.window_size,
                )
            self._scope_successes = {
                scope: deque(
                    tuple(samples)[-settings.window_size :],
                    maxlen=settings.window_size,
                )
                for scope, samples in self._scope_successes.items()
            }

    def apply_fleet_hint(self, hint: FleetHint) -> bool:
        """Accept only an authentic fresh hint and never change local history."""
        now = self._now()
        verifier = self._hint_verifier
        if verifier is None or hint.published_at > now or hint.expires_at <= now:
            return False
        try:
            authentic = verifier(hint)
        except Exception:  # noqa: BLE001
            return False
        if not authentic:
            return False
        with self._lock:
            now = self._now()
            if hint.published_at > now or hint.expires_at <= now:
                return False
            current = self._hints.get(hint.key)
            if current is not None and current.published_at >= hint.published_at:
                return False
            self._hints[hint.key] = hint
        return True

    def acquire(self, scope: HealthScope) -> HealthPermit:
        """Permit a healthy route or hold all matching half-open probe slots."""
        now = self._now()
        with self._lock:
            self._discard_expired_hints(now)
            matching_hints = tuple(
                hint for key, hint in self._hints.items() if key.scope == scope
            )
            if matching_hints:
                local_probe_times = tuple(
                    circuit.next_probe_at
                    for key, circuit in self._circuits.items()
                    if key.scope == scope and circuit.next_probe_at is not None
                )
                hint_expiries = tuple(hint.expires_at for hint in matching_hints)
                next_probe = max(hint_expiries + local_probe_times)
                return HealthPermit(
                    allowed=False,
                    scope=scope,
                    reason="fleet_hint_open",
                    next_probe_at=next_probe,
                )
            matching = tuple(
                (key, circuit)
                for key, circuit in self._circuits.items()
                if key.scope == scope and circuit.state is not CircuitState.CLOSED
            )
            blocked_until = tuple(
                circuit.next_probe_at
                for _key, circuit in matching
                if circuit.state is CircuitState.OPEN
                and circuit.next_probe_at is not None
                and now < circuit.next_probe_at
            )
            if blocked_until:
                return HealthPermit(
                    allowed=False,
                    scope=scope,
                    reason="local_circuit_open",
                    next_probe_at=max(blocked_until),
                )
            if any(
                circuit.state is CircuitState.HALF_OPEN
                and len(circuit.active_probe_tokens) >= self._settings.probe_limit
                for _key, circuit in matching
            ):
                return HealthPermit(
                    allowed=False,
                    scope=scope,
                    reason="half_open_probe_limit",
                )
            probe_keys: list[CircuitKey] = []
            decision_token = object()
            for key, circuit in matching:
                if circuit.state is CircuitState.OPEN:
                    self._transition(circuit, CircuitState.HALF_OPEN, now)
                circuit.active_probe_tokens.add(decision_token)
                probe_keys.append(key)
            owned_probe_keys = tuple(probe_keys)
            self._active_decisions[decision_token] = (scope, owned_probe_keys)
            return HealthPermit(
                allowed=True,
                scope=scope,
                probe_keys=owned_probe_keys,
                _decision_token=decision_token,
            )

    def acquire_plan(self, plan: AttemptPlan, *, operation: str) -> HealthPermit:
        """Make one health decision from an immutable route snapshot."""
        return self.acquire(self.scope_for_plan(plan, operation=operation))

    def abandon(self, permit: HealthPermit) -> None:
        """Consume one decision and release slots without a health sample."""
        with self._lock:
            self._consume_decision(permit)

    def record_plan_result(
        self,
        plan: AttemptPlan,
        result: AdapterResult,
        permit: HealthPermit,
        *,
        operation: str,
    ) -> None:
        """Record only exact provider evidence from a dispatched attempt."""
        now = self._now()
        scope = self.scope_for_plan(plan, operation=operation)
        if permit.scope != scope or not permit.allowed:
            message = "The health permit does not match the provider attempt."
            raise ValueError(message)
        failure_class = _provider_failure_class(plan, result)
        with self._lock:
            if not self._consume_decision(permit):
                message = "The health decision permit is no longer active."
                raise ValueError(message)
            if result.failure is None:
                self._record_scope_success(scope, now)
                for key, circuit in tuple(self._circuits.items()):
                    if key.scope == scope:
                        self._record_success(circuit, now)
                return
            if failure_class is None:
                return
            key = CircuitKey(scope, failure_class)
            circuit = self._circuits.setdefault(
                key, self._new_circuit(now, scope=scope)
            )
            self._record_failure(circuit, now)

    def reset(self, key: CircuitKey) -> None:
        """Reset one exact local circuit without changing a fleet hint."""
        now = self._now()
        with self._lock:
            self._circuits[key] = self._new_circuit(now)

    def request_probe(self, key: CircuitKey) -> bool:
        """Make one exact local open circuit ready for a normal controlled probe."""
        now = self._now()
        with self._lock:
            circuit = self._circuits.get(key)
            if circuit is None or circuit.state is CircuitState.CLOSED:
                return False
            circuit.next_probe_at = now
            return True

    def inspect(self, scope: HealthScope | None = None) -> tuple[CircuitSnapshot, ...]:
        """Return deterministic safe state without authentication tags."""
        now = self._now()
        with self._lock:
            self._discard_expired_hints(now)
            keys = set(self._circuits) | set(self._hints)
            if scope is not None:
                keys = {key for key in keys if key.scope == scope}
            snapshots = [self._snapshot(key, now) for key in keys]
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (
                    item.key.scope.provider_instance_id,
                    item.key.scope.provider_instance_generation,
                    item.key.scope.provider_model_route_id,
                    item.key.scope.route_generation,
                    item.key.scope.credential_id,
                    item.key.scope.credential_generation,
                    item.key.scope.operation,
                    tuple(sorted(item.key.scope.required_capabilities)),
                    item.key.failure_class.value,
                ),
            )
        )

    @staticmethod
    def scope_for_plan(plan: AttemptPlan, *, operation: str) -> HealthScope:
        """Derive only the accepted narrow route and operation identity."""
        raw_capabilities = plan.candidate_policy.get("required_capabilities", ())
        if not isinstance(raw_capabilities, (list, tuple, frozenset)):
            message = "The required health capabilities are invalid."
            raise TypeError(message)
        capabilities = frozenset(raw_capabilities)
        if any(not isinstance(value, str) for value in capabilities):
            message = "The required health capabilities are invalid."
            raise TypeError(message)
        return HealthScope(
            provider_instance_id=plan.provider_instance_id,
            provider_instance_generation=plan.provider_instance_generation,
            provider_model_route_id=plan.provider_model_route_id,
            route_generation=plan.route_generation,
            credential_id=plan.credential_id,
            credential_generation=plan.credential_generation,
            operation=operation,
            required_capabilities=capabilities,
        )

    def _record_success(self, circuit: _Circuit, now: datetime) -> None:
        sample = _Sample(succeeded=True, occurred_at=now)
        circuit.samples.append(sample)
        if circuit.state is not CircuitState.CLOSED:
            self._transition(circuit, CircuitState.CLOSED, now)
            circuit.samples.clear()
            circuit.samples.append(sample)
            circuit.open_count = 0
        circuit.next_probe_at = None

    def _record_scope_success(self, scope: HealthScope, now: datetime) -> None:
        samples = self._scope_successes.setdefault(
            scope, deque(maxlen=self._settings.window_size)
        )
        samples.append(_Sample(succeeded=True, occurred_at=now))

    def _record_failure(self, circuit: _Circuit, now: datetime) -> None:
        circuit.samples.append(_Sample(succeeded=False, occurred_at=now))
        if circuit.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}:
            self._open(circuit, now)
            return
        failures = sum(not sample.succeeded for sample in circuit.samples)
        if (
            len(circuit.samples) >= self._settings.minimum_samples
            and failures / len(circuit.samples) >= self._settings.failure_threshold
        ):
            self._open(circuit, now)

    def _open(self, circuit: _Circuit, now: datetime) -> None:
        circuit.open_count += 1
        open_seconds = self._settings.open_duration.total_seconds()
        maximum_seconds = self._settings.maximum_backoff.total_seconds()
        maximum_exponent = ceil(log2(maximum_seconds / open_seconds))
        base_seconds = min(
            open_seconds * (2 ** min(circuit.open_count - 1, maximum_exponent)),
            maximum_seconds,
        )
        jitter_bound = min(
            base_seconds * self._settings.jitter_ratio,
            maximum_seconds - base_seconds,
        )
        jitter_seconds = self._jitter(jitter_bound) if jitter_bound else 0.0
        if not 0 <= jitter_seconds <= jitter_bound:
            message = "The health jitter source returned an unsafe value."
            raise ValueError(message)
        self._transition(circuit, CircuitState.OPEN, now)
        circuit.next_probe_at = now + timedelta(seconds=base_seconds + jitter_seconds)

    @staticmethod
    def _transition(circuit: _Circuit, state: CircuitState, now: datetime) -> None:
        if circuit.state is not state:
            circuit.state = state
            circuit.last_state_change = now

    def _consume_decision(self, permit: HealthPermit) -> bool:
        token = permit._decision_token  # noqa: SLF001
        if token is None:
            return False
        owned = self._active_decisions.get(token)
        if owned != (permit.scope, permit.probe_keys):
            return False
        del self._active_decisions[token]
        self._release_probe_slots(permit)
        return True

    def _release_probe_slots(self, permit: HealthPermit) -> None:
        token = permit._decision_token  # noqa: SLF001
        if token is None:
            return
        for key in permit.probe_keys:
            circuit = self._circuits.get(key)
            if circuit is not None:
                circuit.active_probe_tokens.discard(token)

    def _snapshot(self, key: CircuitKey, now: datetime) -> CircuitSnapshot:
        circuit = self._circuits.get(key)
        hint = self._hints.get(key)
        samples = () if circuit is None else tuple(circuit.samples)
        return CircuitSnapshot(
            key=key,
            local_state=(CircuitState.CLOSED if circuit is None else circuit.state),
            sample_count=len(samples),
            failure_count=sum(not sample.succeeded for sample in samples),
            sample_started_at=None if not samples else samples[0].occurred_at,
            sample_ended_at=None if not samples else samples[-1].occurred_at,
            next_probe_at=(None if circuit is None else circuit.next_probe_at),
            active_probes=(0 if circuit is None else len(circuit.active_probe_tokens)),
            last_state_change=(
                hint.published_at
                if circuit is None and hint is not None
                else now
                if circuit is None
                else circuit.last_state_change
            ),
            fleet_hint_active=hint is not None,
            fleet_hint_sample_started_at=(
                None if hint is None else hint.sample_started_at
            ),
            fleet_hint_sample_ended_at=(None if hint is None else hint.sample_ended_at),
            fleet_hint_published_at=(None if hint is None else hint.published_at),
            fleet_hint_expires_at=(None if hint is None else hint.expires_at),
        )

    def _discard_expired_hints(self, now: datetime) -> None:
        self._hints = {
            key: hint for key, hint in self._hints.items() if hint.expires_at > now
        }

    def _new_circuit(
        self, now: datetime, *, scope: HealthScope | None = None
    ) -> _Circuit:
        samples: deque[_Sample] = deque(maxlen=self._settings.window_size)
        if scope is not None:
            samples.extend(self._scope_successes.get(scope, ()))
        return _Circuit(
            CircuitState.CLOSED,
            now,
            samples=samples,
        )

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now)
        return now


def _provider_failure_class(
    plan: AttemptPlan, result: AdapterResult
) -> ProviderFailureClass | None:
    failure = result.failure
    if failure is None:
        return None
    error = failure.error
    exact_scope_id = {
        ErrorScope.ATTEMPT: plan.attempt_id,
        ErrorScope.PROVIDER_MODEL_ROUTE: plan.provider_model_route_id,
        ErrorScope.PROVIDER_INSTANCE: plan.provider_instance_id,
        ErrorScope.CREDENTIAL: plan.credential_id,
    }.get(error.affected_scope)
    if exact_scope_id is None or failure.affected_scope_id != exact_scope_id:
        return None
    if error.error_class is TerminalErrorClass.AUTHENTICATION:
        return (
            ProviderFailureClass.CREDENTIAL
            if error.affected_scope is ErrorScope.CREDENTIAL
            else None
        )
    return {
        TerminalErrorClass.TIMEOUT: ProviderFailureClass.TIMEOUT,
        TerminalErrorClass.TRANSPORT: ProviderFailureClass.TRANSPORT,
        TerminalErrorClass.PROVIDER_UNAVAILABLE: ProviderFailureClass.SERVER,
        TerminalErrorClass.RATE_LIMIT: ProviderFailureClass.RATE_LIMIT,
        TerminalErrorClass.INVALID_PROVIDER_RESPONSE: (
            ProviderFailureClass.INVALID_RESPONSE
        ),
    }.get(error.error_class)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        message = "Health circuit time must include a time zone."
        raise ValueError(message)

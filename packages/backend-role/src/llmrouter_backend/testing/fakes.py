"""Deterministic fakes for external Router boundaries."""
# ruff: noqa: EM101, EM102, N818, TRY003

from __future__ import annotations

import hashlib
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type OutcomeKind = Literal[
    "success", "retry", "timeout", "interruption", "cancellation"
]
MAXIMUM_ENDPOINTS = 8


class FakeServiceError(RuntimeError):
    """Base error for a controlled fake-service result."""


class RetryableFailure(FakeServiceError):
    """A caller can try a different eligible endpoint."""


class TimedOut(RetryableFailure):
    """The configured attempt time expired."""


class Interrupted(FakeServiceError):
    """A committed response ended before normal completion."""


class Cancelled(FakeServiceError):
    """A controlled cancellation stopped the operation."""


class StaleState(FakeServiceError):
    """An expected state revision does not match."""


class DuplicateEffect(FakeServiceError):
    """One operation identity tried to create a different effect."""


class TakeoverRejected(FakeServiceError):
    """An owner tried to write with an old fencing epoch."""


class UncertainEffect(FakeServiceError):
    """An external effect cannot run again after uncertain takeover."""


@dataclass
class FakeClock:
    """A clock that moves only when a test moves it."""

    current: datetime = field(default_factory=lambda: datetime(2026, 8, 13, tzinfo=UTC))

    def now(self) -> datetime:
        """Return the current controlled time."""
        return self.current

    def advance(self, duration: timedelta) -> datetime:
        """Move time forward by a nonnegative duration."""
        if duration < timedelta(0):
            raise ValueError("A fake clock cannot move back.")
        self.current += duration
        return self.current

    def sleep(self, seconds: float) -> datetime:
        """Advance without a real wait."""
        return self.advance(timedelta(seconds=seconds))


@dataclass(frozen=True)
class Outcome:
    """One controlled endpoint result."""

    kind: OutcomeKind
    value: JsonValue = None
    delay_seconds: float = 0
    committed: bool = False


@dataclass(frozen=True)
class EndpointCall:
    """One recorded endpoint call."""

    operation_id: str
    input_document: JsonValue
    started_at: datetime
    timeout_seconds: float


class FakeEndpoint:
    """Return scripted outcomes and record each call."""

    def __init__(
        self,
        name: str,
        outcomes: list[Outcome],
        *,
        clock: FakeClock | None = None,
        service_id: str | None = None,
        healthy: bool = True,
    ) -> None:
        """Create an endpoint with an ordered outcome script."""
        self.name = name
        self.clock = clock or FakeClock()
        self.service_id = service_id
        self.healthy = healthy
        self._outcomes = deque(deepcopy(outcomes))
        self._calls: list[EndpointCall] = []

    @property
    def calls(self) -> tuple[EndpointCall, ...]:
        """Return the append-only call observation log."""
        return tuple(deepcopy(self._calls))

    def execute(
        self, operation_id: str, input_document: JsonValue, *, timeout_seconds: float
    ) -> JsonValue:
        """Execute one scripted result without I/O or a real wait."""
        self._calls.append(
            EndpointCall(
                operation_id=operation_id,
                input_document=deepcopy(input_document),
                started_at=self.clock.now(),
                timeout_seconds=timeout_seconds,
            )
        )
        if not self._outcomes:
            raise AssertionError(f"Endpoint {self.name} has no scripted outcome.")
        outcome = self._outcomes.popleft()
        elapsed = min(outcome.delay_seconds, timeout_seconds)
        self.clock.sleep(elapsed)
        if outcome.delay_seconds > timeout_seconds or outcome.kind == "timeout":
            raise TimedOut(f"Endpoint {self.name} timed out.")
        if outcome.kind == "retry":
            raise RetryableFailure(f"Endpoint {self.name} requested retry.")
        if outcome.kind == "interruption" or (
            outcome.committed and outcome.kind != "success"
        ):
            raise Interrupted(f"Endpoint {self.name} interrupted committed output.")
        if outcome.kind == "cancellation":
            raise Cancelled(f"Endpoint {self.name} was cancelled.")
        return deepcopy(outcome.value)


class FakeFailoverService:
    """Try scripted endpoints in order before a commit boundary."""

    def __init__(self, endpoints: list[FakeEndpoint]) -> None:
        """Create one ordered endpoint chain."""
        if not endpoints:
            raise ValueError("A failover chain needs one endpoint.")
        if len(endpoints) > MAXIMUM_ENDPOINTS:
            raise ValueError("A failover chain permits no more than eight endpoints.")
        self.endpoints = tuple(endpoints)
        self._observations: list[tuple[str, str]] = []

    @property
    def observations(self) -> tuple[tuple[str, str], ...]:
        """Return each ordered eligibility and attempt decision."""
        return tuple(self._observations)

    @property
    def attempted_endpoints(self) -> list[str]:
        """Return endpoint identities that received an attempt."""
        return [name for name, decision in self._observations if decision == "attempt"]

    def execute(
        self,
        operation_id: str,
        input_document: JsonValue,
        *,
        timeout_seconds: float,
        service_id: str | None = None,
    ) -> JsonValue:
        """Return the first successful result or the final retryable error."""
        last_error: RetryableFailure | None = None
        for endpoint in self.endpoints:
            if endpoint.service_id is not None and endpoint.service_id != service_id:
                self._observations.append((endpoint.name, "wrong_identity"))
                continue
            if not endpoint.healthy:
                self._observations.append((endpoint.name, "unhealthy"))
                continue
            self._observations.append((endpoint.name, "attempt"))
            try:
                return endpoint.execute(
                    operation_id, input_document, timeout_seconds=timeout_seconds
                )
            except RetryableFailure as error:
                last_error = error
        if last_error is None:
            raise AssertionError("The failover chain made no attempt.")
        raise last_error


class FakeModelService(FakeFailoverService):
    """A deterministic provider-neutral model service."""

    def complete(
        self,
        request_id: str,
        request: JsonValue,
        *,
        timeout_seconds: float = 120,
        service_id: str | None = None,
    ) -> JsonValue:
        """Run one model request through the controlled endpoint chain."""
        return self.execute(
            request_id,
            request,
            timeout_seconds=timeout_seconds,
            service_id=service_id,
        )


class FakeToolService(FakeFailoverService):
    """A deterministic shared-tool or business-tool service."""

    def call(
        self,
        operation_id: str,
        input_document: JsonValue,
        *,
        timeout_seconds: float = 120,
        service_id: str | None = None,
    ) -> JsonValue:
        """Run one tool operation through the controlled endpoint chain."""
        return self.execute(
            operation_id,
            input_document,
            timeout_seconds=timeout_seconds,
            service_id=service_id,
        )


@dataclass(frozen=True)
class FakeToken:
    """One short-lived fake machine token."""

    value: str
    service_id: str
    workspace_id: str | None
    audience: str
    operation: str
    expires_at: datetime


class FakeIdentityService:
    """Issue and verify deterministic short-lived identities."""

    def __init__(self, *, clock: FakeClock | None = None) -> None:
        """Create an empty identity service."""
        self.clock = clock or FakeClock()
        self._tokens: dict[str, FakeToken] = {}
        self._sequence = 0

    def issue(
        self,
        *,
        service_id: str,
        audience: str,
        operation: str,
        workspace_id: str | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> str:
        """Issue a deterministic token for one exact operation."""
        self._sequence += 1
        value = f"fake-token-{self._sequence}"
        self._tokens[value] = FakeToken(
            value=value,
            service_id=service_id,
            workspace_id=workspace_id,
            audience=audience,
            operation=operation,
            expires_at=self.clock.now() + lifetime,
        )
        return value

    def authenticate(
        self,
        value: str,
        *,
        audience: str,
        operation: str,
        service_id: str,
        workspace_id: str | None = None,
    ) -> FakeToken:
        """Verify the exact fake scope before test record access."""
        token = self._tokens.get(value)
        if token is None or token.expires_at <= self.clock.now():
            raise PermissionError("The token is absent or expired.")
        expected = (service_id, workspace_id, audience, operation)
        actual = (
            token.service_id,
            token.workspace_id,
            token.audience,
            token.operation,
        )
        if actual != expected:
            raise PermissionError("The token scope does not match.")
        return token


@dataclass(frozen=True)
class FakeObject:
    """One immutable fake object."""

    content: bytes
    sha256: str


class FakeObjectStore:
    """Store immutable bytes without file or network access."""

    def __init__(self) -> None:
        """Create an empty object store."""
        self._objects: dict[str, FakeObject] = {}

    def put(self, object_id: str, content: bytes, *, sha256: str) -> FakeObject:
        """Store verified immutable content with idempotent duplicate delivery."""
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            raise ValueError("The object digest does not match.")
        candidate = FakeObject(content=content, sha256=sha256)
        current = self._objects.get(object_id)
        if current is not None and current != candidate:
            raise DuplicateEffect("The object identity has different content.")
        if current is not None:
            return current
        self._objects[object_id] = candidate
        return candidate

    def get(self, object_id: str) -> FakeObject:
        """Return one stored object."""
        return self._objects[object_id]


@dataclass(frozen=True)
class LedgerEvent:
    """One accepted fake ledger event."""

    event_id: str
    document: JsonValue
    revision: int
    owner_epoch: int


class FakeLedger:
    """Enforce revisions, duplicate delivery, and fenced takeover."""

    def __init__(self) -> None:
        """Create an empty ledger at owner epoch one."""
        self.revision = 0
        self.owner_epoch = 1
        self._events: dict[str, LedgerEvent] = {}
        self.effect_states: dict[str, str] = {}

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        """Return events in accepted revision order."""
        ordered = sorted(self._events.values(), key=lambda event: event.revision)
        return tuple(deepcopy(ordered))

    def append(
        self,
        event_id: str,
        document: JsonValue,
        *,
        expected_revision: int,
        owner_epoch: int,
    ) -> LedgerEvent:
        """Append one event with idempotent duplicate delivery."""
        existing = self._events.get(event_id)
        if existing is not None:
            if existing.document != document:
                raise DuplicateEffect("The event identity has a different document.")
            return existing
        if owner_epoch != self.owner_epoch:
            raise TakeoverRejected("The ledger owner epoch is stale.")
        if expected_revision != self.revision:
            raise StaleState("The ledger revision is stale.")
        self.revision += 1
        event = LedgerEvent(event_id, deepcopy(document), self.revision, owner_epoch)
        self._events[event_id] = event
        return event

    def takeover(self, *, expected_owner_epoch: int) -> int:
        """Fence the old owner and return the new epoch."""
        if expected_owner_epoch != self.owner_epoch:
            raise TakeoverRejected("The takeover epoch is stale.")
        for effect_id, state in self.effect_states.items():
            if state == "intent_recorded":
                self.effect_states[effect_id] = "uncertain"
        self.owner_epoch += 1
        return self.owner_epoch

    def record_effect_intent(self, effect_id: str, *, owner_epoch: int) -> None:
        """Record an external-effect intent before execution."""
        if owner_epoch != self.owner_epoch:
            raise TakeoverRejected("The effect owner epoch is stale.")
        current = self.effect_states.get(effect_id)
        if current == "uncertain":
            raise UncertainEffect("An uncertain effect cannot run again automatically.")
        if current == "intent_recorded":
            return
        if current is not None:
            raise DuplicateEffect("The effect identity already has a result.")
        self.effect_states[effect_id] = "intent_recorded"


class FakeConfigurationService:
    """Expose current or stale configuration from controlled time."""

    MAXIMUM_STALE_AGE = timedelta(hours=24)

    def __init__(self, *, clock: FakeClock | None = None) -> None:
        """Create a configuration service with one current revision."""
        self.clock = clock or FakeClock()
        self.fetched_at = self.clock.now()
        self.revision = "revision-1"

    def read(self) -> tuple[str, str]:
        """Return revision and current state for the controlled age."""
        age = self.clock.now() - self.fetched_at
        if age > self.MAXIMUM_STALE_AGE:
            raise StaleState("Configuration is older than 24 hours.")
        return self.revision, "current" if age == timedelta(0) else "stale"


class FakeCancellationReconciler:
    """Make unresolved cancellation terminal after the accepted limit."""

    LIMIT = timedelta(minutes=10)

    def __init__(self, *, clock: FakeClock | None = None) -> None:
        """Create a reconciler with no active request."""
        self.clock = clock or FakeClock()
        self.requested_at: datetime | None = None

    def request(self) -> str:
        """Start bounded reconciliation."""
        self.requested_at = self.clock.now()
        return "cancel_requested"

    def state(self, *, stopped: bool = False) -> str:
        """Return cancelled, pending, or terminal uncertain state."""
        if self.requested_at is None:
            raise StaleState("Cancellation was not requested.")
        if stopped:
            return "cancelled"
        if self.clock.now() - self.requested_at >= self.LIMIT:
            return "uncertain"
        return "cancel_requested"

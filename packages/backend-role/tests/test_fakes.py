"""Tests for deterministic external-service fakes."""

from datetime import UTC, datetime, timedelta

import pytest
from llmrouter_backend.testing import (
    Cancelled,
    DuplicateEffect,
    FakeCancellationReconciler,
    FakeClock,
    FakeConfigurationService,
    FakeEndpoint,
    FakeFailoverService,
    FakeIdentityService,
    FakeLedger,
    FakeModelService,
    FakeObjectStore,
    FakeToolService,
    Interrupted,
    JsonValue,
    Outcome,
    StaleState,
    TakeoverRejected,
    UncertainEffect,
)


def test_clock_moves_without_a_real_wait() -> None:
    """The fake clock moves only by the requested duration."""
    clock = FakeClock(datetime(2026, 8, 13, tzinfo=UTC))
    assert clock.sleep(2.5) == datetime(2026, 8, 13, 0, 0, 2, 500000, tzinfo=UTC)
    with pytest.raises(ValueError, match="cannot move back"):
        clock.advance(timedelta(seconds=-1))


def test_model_success_and_retry_are_deterministic() -> None:
    """A retryable first candidate moves to the next model endpoint."""
    first = FakeEndpoint("first", [Outcome("retry")])
    second = FakeEndpoint("second", [Outcome("success", {"text": "ok"})])
    model = FakeModelService([first, second])
    assert model.complete("request-1", {"input": "hello"}) == {"text": "ok"}
    assert model.attempted_endpoints == ["first", "second"]


def test_timeout_uses_controlled_time_and_fails_over() -> None:
    """An attempt timeout advances the clock and permits failover."""
    clock = FakeClock()
    slow = FakeEndpoint(
        "slow", [Outcome("success", "late", delay_seconds=10)], clock=clock
    )
    fast = FakeEndpoint("fast", [Outcome("success", "ok")], clock=clock)
    service = FakeFailoverService([slow, fast])
    assert service.execute("operation-1", {}, timeout_seconds=3) == "ok"
    assert clock.now() == datetime(2026, 8, 13, 0, 0, 3, tzinfo=UTC)


def test_interruption_does_not_fail_over_after_commit() -> None:
    """An interrupted committed result does not call the next endpoint."""
    first = FakeEndpoint("first", [Outcome("interruption", committed=True)])
    second = FakeEndpoint("second", [Outcome("success", "unsafe repeat")])
    service = FakeModelService([first, second])
    with pytest.raises(Interrupted):
        service.complete("request-1", {})
    assert second.calls == ()


def test_cancellation_does_not_fail_over() -> None:
    """Cancellation stops the tool chain."""
    first = FakeEndpoint("first", [Outcome("cancellation")])
    second = FakeEndpoint("second", [Outcome("success", "unsafe repeat")])
    service = FakeToolService([first, second])
    with pytest.raises(Cancelled):
        service.call("tool-1", {})
    assert second.calls == ()


def test_identity_rejects_wrong_scope_and_expiry() -> None:
    """The fake identity service applies exact scope and controlled expiry."""
    clock = FakeClock()
    identity = FakeIdentityService(clock=clock)
    token = identity.issue(
        service_id="service-1", audience="data_plane", operation="model.create"
    )
    with pytest.raises(PermissionError, match="scope"):
        identity.authenticate(
            token,
            service_id="service-2",
            audience="data_plane",
            operation="model.create",
        )
    clock.advance(timedelta(minutes=5))
    with pytest.raises(PermissionError, match="expired"):
        identity.authenticate(
            token,
            service_id="service-1",
            audience="data_plane",
            operation="model.create",
        )


def test_object_store_accepts_equal_duplicate_delivery() -> None:
    """Equal object delivery is idempotent and changed delivery fails."""
    store = FakeObjectStore()
    digest = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    first = store.put("object-1", b"hello", sha256=digest)
    assert store.put("object-1", b"hello", sha256=digest) is first
    changed_digest = "486ea46224d1bb4fb680f34f7c9ad96a8f24ec88be73ea8e5a6c65260e9cb8a7"
    with pytest.raises(DuplicateEffect):
        store.put("object-1", b"world", sha256=changed_digest)


def test_ledger_rejects_stale_state_and_changed_duplicate() -> None:
    """The ledger protects revision and operation identity."""
    ledger = FakeLedger()
    first = ledger.append("event-1", {"amount": 1}, expected_revision=0, owner_epoch=1)
    assert (
        ledger.append("event-1", {"amount": 1}, expected_revision=0, owner_epoch=1)
        is first
    )
    with pytest.raises(DuplicateEffect):
        ledger.append("event-1", {"amount": 2}, expected_revision=1, owner_epoch=1)
    with pytest.raises(StaleState):
        ledger.append("event-2", {}, expected_revision=0, owner_epoch=1)


def test_takeover_fences_the_old_owner() -> None:
    """A new epoch rejects effects from the old owner."""
    ledger = FakeLedger()
    ledger.record_effect_intent("effect-1", owner_epoch=1)
    new_epoch = ledger.takeover(expected_owner_epoch=1)
    assert new_epoch == ledger.owner_epoch
    with pytest.raises(TakeoverRejected):
        ledger.append("event-1", {}, expected_revision=0, owner_epoch=1)
    event = ledger.append("event-1", {}, expected_revision=0, owner_epoch=new_epoch)
    assert event.owner_epoch == new_epoch
    assert ledger.effect_states == {"effect-1": "uncertain"}
    with pytest.raises(UncertainEffect, match="cannot run again"):
        ledger.record_effect_intent("effect-1", owner_epoch=new_epoch)


def test_scoped_model_endpoint_and_observation_are_deterministic() -> None:
    """The model fake passes identity and keeps an input snapshot."""
    request: dict[str, JsonValue] = {"input": {"text": "before"}}
    endpoint = FakeEndpoint(
        "scoped",
        [Outcome("success", {"text": "ok"})],
        service_id="service-1",
    )
    model = FakeModelService([endpoint])
    assert model.complete("request-1", request, service_id="service-1") == {
        "text": "ok"
    }
    request["input"] = {"text": "after"}
    assert endpoint.calls[0].input_document == {"input": {"text": "before"}}


def test_ninth_endpoint_is_rejected() -> None:
    """The fake enforces the accepted eight-attempt maximum."""
    endpoints = [FakeEndpoint(str(index), [Outcome("retry")]) for index in range(9)]
    with pytest.raises(ValueError, match="eight"):
        FakeFailoverService(endpoints)


def test_cancel_reconciliation_finishes_uncertain_at_ten_minutes() -> None:
    """Unconfirmed cancellation becomes uncertain at the accepted limit."""
    clock = FakeClock()
    reconciler = FakeCancellationReconciler(clock=clock)
    assert reconciler.request() == "cancel_requested"
    clock.advance(timedelta(minutes=9, seconds=59))
    assert reconciler.state() == "cancel_requested"
    clock.advance(timedelta(seconds=1))
    assert reconciler.state() == "uncertain"


def test_configuration_expires_after_twenty_four_hours() -> None:
    """Stale configuration remains usable only through its accepted limit."""
    clock = FakeClock()
    configuration = FakeConfigurationService(clock=clock)
    clock.advance(timedelta(hours=24))
    assert configuration.read() == ("revision-1", "stale")
    clock.advance(timedelta(microseconds=1))
    with pytest.raises(StaleState, match="24 hours"):
        configuration.read()


def test_endpoint_observation_log_is_complete_and_ordered() -> None:
    """The fake records endpoint identity, input, time, and timeout in order."""
    clock = FakeClock()
    endpoint = FakeEndpoint(
        "endpoint-1",
        [Outcome("success", "one"), Outcome("success", "two")],
        clock=clock,
    )
    endpoint.execute("one", {"index": 1}, timeout_seconds=4)
    endpoint.execute("two", {"index": 2}, timeout_seconds=5)
    assert [call.operation_id for call in endpoint.calls] == ["one", "two"]
    assert [call.input_document for call in endpoint.calls] == [
        {"index": 1},
        {"index": 2},
    ]
    assert [call.timeout_seconds for call in endpoint.calls] == [4, 5]
    assert all(call.started_at == clock.now() for call in endpoint.calls)
    observed = endpoint.calls[0].input_document
    assert isinstance(observed, dict)
    observed["index"] = 3
    assert endpoint.calls[0].input_document == {"index": 1}


def test_failover_skips_wrong_identity_and_unhealthy_endpoint_in_order() -> None:
    """Eligibility decisions and the final attempt have one exact ordered log."""
    wrong = FakeEndpoint(
        "wrong", [Outcome("success", "unsafe")], service_id="service-2"
    )
    unhealthy = FakeEndpoint("unhealthy", [Outcome("success", "unsafe")], healthy=False)
    eligible = FakeEndpoint(
        "eligible", [Outcome("success", "ok")], service_id="service-1"
    )
    service = FakeFailoverService([wrong, unhealthy, eligible])
    assert (
        service.execute("operation-1", {}, timeout_seconds=5, service_id="service-1")
        == "ok"
    )
    assert service.observations == (
        ("wrong", "wrong_identity"),
        ("unhealthy", "unhealthy"),
        ("eligible", "attempt"),
    )
    assert wrong.calls == ()
    assert unhealthy.calls == ()

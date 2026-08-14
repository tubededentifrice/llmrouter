"""PostgreSQL execution lifecycle, stream, and cancellation tests."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import uuid
from collections.abc import Callable  # noqa: TC003
from datetime import UTC, datetime, timedelta

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
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    AdapterStopEvidence,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
    make_event,
)
from psycopg.rows import dict_row

from .helpers import (
    CONFIGURATION_ID,
    FIXTURE_ROUTE_ID,
    OTHER_SERVICE_ID,
    OTHER_WORKSPACE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    seed_request_target,
    seed_scope,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
TARGET = ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
RUN_ROW_ID = "0198a080-0000-7000-8000-000000000060"
RUN_ID = "0198a080-0000-7000-8000-000000000061"
RUN_TARGET = ExecutionTarget(ExecutionKind.AGENT_RUN, RUN_ID)
PRICE_VERSION_ID = "0198a080-0000-7000-8000-000000000072"


def _context(
    operation: str,
    *,
    mutation: bool,
    service_id: str = SERVICE_ID,
    workspace_id: str = WORKSPACE_ID,
) -> RequestContext:
    return RequestContext(
        request_id="transport-request",
        actor_kind=PrincipalKind.SERVICE,
        actor_id=service_id,
        authority_class=AuthorityClass.SERVICE,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.DATA_PLANE,
        operation=operation,
        scope=Scope(service_id, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=mutation,
    )


def _system_context(operation: str) -> RequestContext:
    return RequestContext(
        request_id="system-execution",
        actor_kind=PrincipalKind.SYSTEM,
        actor_id="worker",
        authority_class=AuthorityClass.SYSTEM,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=None,
        operation=operation,
        scope=Scope(SERVICE_ID, WORKSPACE_ID),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=True,
    )


@pytest.fixture
def repository(database_url: str) -> PostgresExecutionRepository:  # noqa: D103
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        seed_request_target(connection)
        connection.execute(
            """INSERT INTO router.logical_requests (
                   row_id, request_id, request_kind, service_id, workspace_id,
                   assignment_id, configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, data_profile, capture_enabled,
                   status_location, cancel_location, events_location
               ) VALUES (%s, %s, 'model', %s, %s,
                         '0198a080-0000-7000-8000-000000000012', %s, 1, %s,
                         'service-data', true, %s, %s, %s)""",
            (
                REQUEST_ROW_ID,
                REQUEST_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
                bytes.fromhex("03" * 32),
                f"/v1/model-requests/{REQUEST_ID}",
                f"/v1/model-requests/{REQUEST_ID}/cancel",
                f"/v1/model-requests/{REQUEST_ID}/events",
            ),
        )
    return PostgresExecutionRepository(database_url)


def test_insert_creates_exact_full_admission_event(  # noqa: D103
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    event = repository.replay(
        _context("model.read", mutation=False), TARGET, after_sequence=0
    )[0]
    envelope = json.loads(event.wire_data)

    assert event.sequence == 1
    assert event.event_name == "request.admitted"
    assert envelope["request_id"] == REQUEST_ID
    assert envelope["payload"]["state"] == "admitted"
    assert envelope["payload"]["state_revision"] == 1
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        request = connection.execute(
            "SELECT * FROM router.logical_requests WHERE row_id = %s",
            (REQUEST_ROW_ID,),
        ).fetchone()
        row = connection.execute(
            """SELECT wire_data, wire_sha256 FROM router.execution_stream_events
               WHERE request_row_id = %s AND sequence = 1""",
            (REQUEST_ROW_ID,),
        ).fetchone()
    assert request is not None
    admission = dict(envelope["payload"]["admission"])
    admitted_at = admission.pop("admitted_at")
    assert datetime.fromisoformat(admitted_at) == request["admitted_at"]
    assert admission == {
        "request_id": REQUEST_ID,
        "status_url": request["status_location"],
        "cancel_url": request["cancel_location"],
        "events_url": request["events_location"],
        "fingerprint_version": "rfc8785-sha256-v1",
        "capture_enabled": request["capture_enabled"],
        "capture_reason": request["capture_reason"],
    }
    assert row is not None
    assert hashlib.sha256(row["wire_data"].encode()).digest() == row["wire_sha256"]


def test_run_admission_has_receipt_parity_and_immutable_baseline(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Return the same required admission receipt fields for an agent run."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.agent_runs (
                   row_id, run_id, service_id, workspace_id,
                   configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, capture_enabled, capture_reason
               ) VALUES (%s, %s, %s, %s, %s, 1, %s, false, 'configured')""",
            (
                RUN_ROW_ID,
                RUN_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
                bytes.fromhex("0a" * 32),
            ),
        )
    read = _context("run.read", mutation=False)
    event = repository.replay(read, RUN_TARGET, after_sequence=0)[0]
    admission = json.loads(event.wire_data)["payload"]["admission"]
    status = repository.status(read, RUN_TARGET)

    assert admission == {
        "request_id": RUN_ID,
        "run_id": RUN_ID,
        "admitted_at": admission["admitted_at"],
        "status_url": f"/v1/agent-runs/{RUN_ID}",
        "cancel_url": f"/v1/agent-runs/{RUN_ID}/cancel",
        "events_url": f"/v1/agent-runs/{RUN_ID}/events",
        "fingerprint_version": "rfc8785-sha256-v1",
        "capture_enabled": False,
        "capture_reason": "configured",
    }
    assert status.admission.state is ExecutionState.ADMITTED
    assert status.admission.state_revision == 1
    assert status.admission.fingerprint_version == "rfc8785-sha256-v1"


def _seed_admitted_run(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.agent_runs (
                   row_id, run_id, service_id, workspace_id,
                   configuration_revision_id, fingerprint_version,
                   fingerprint_sha256, capture_enabled, capture_reason
               ) VALUES (%s, %s, %s, %s, %s, 1, %s, false, 'configured')""",
            (
                RUN_ROW_ID,
                RUN_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                CONFIGURATION_ID,
                bytes.fromhex("0a" * 32),
            ),
        )
        connection.execute(
            """INSERT INTO router.control_epochs (epoch, fencing_evidence)
               VALUES (1, 'test')"""
        )
        connection.execute(
            """INSERT INTO router.run_leases (
                   run_row_id, owner_node_id, control_epoch, owner_epoch,
                   lease_generation, expires_at
               ) VALUES (%s, %s, 1, 1, 1,
                         transaction_timestamp() + interval '1 hour')""",
            (RUN_ROW_ID, "0198a080-0000-7000-8000-000000000062"),
        )


def _seed_running_run(
    database_url: str,
    repository: PostgresExecutionRepository,
    operation_ids: tuple[str, ...],
) -> None:
    _seed_admitted_run(database_url)
    repository.transition(
        _context("run.create", mutation=True),
        RUN_TARGET,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
        owner_epoch=1,
    )
    with psycopg.connect(database_url) as connection:
        for ordinal, operation_id in enumerate(operation_ids, start=1):
            connection.execute(
                """INSERT INTO router.effect_intents (
                       id, run_row_id, owner_epoch, operation_identity, effect_kind,
                       request_fingerprint, state
                   ) VALUES (%s, %s, 1, %s, 'business-tool', %s, 'intent')""",
                (
                    f"0198a080-0000-7000-8000-{ordinal:012d}",
                    RUN_ROW_ID,
                    operation_id,
                    bytes.fromhex("0b" * 32),
                ),
            )


def _stop(operation_id: str, *, confirmed: bool) -> Callable[[], AdapterStopEvidence]:
    def result() -> AdapterStopEvidence:
        return AdapterStopEvidence(
            operation_id=operation_id,
            supported=True,
            stop_requested=True,
            confirmed_stopped=confirmed,
        )

    return result


def _insert_provider_attempt(
    connection: psycopg.Connection[object], *, attempt_id: str, attempt_number: int
) -> None:
    connection.execute(
        """INSERT INTO router.provider_attempts (
               id, request_row_id, service_id, workspace_id, attempt_number,
               provider_model_route_id, route_generation,
               assignment_revision_id, price_version_id, state
           ) VALUES (
               %s, %s, %s, %s, %s, %s,
               (SELECT generation FROM router.provider_model_routes WHERE id = %s),
               %s, %s, 'started'
           )""",
        (
            attempt_id,
            REQUEST_ROW_ID,
            SERVICE_ID,
            WORKSPACE_ID,
            attempt_number,
            FIXTURE_ROUTE_ID,
            FIXTURE_ROUTE_ID,
            CONFIGURATION_ID,
            PRICE_VERSION_ID,
        ),
    )


def test_cancellation_combines_proof_across_retries(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Finish cancellation from cumulative proof for two active operations."""
    _seed_running_run(database_url, repository, ("effect-one",))
    cancel = _context("run.cancel", mutation=True)
    first = repository.cancel(
        cancel,
        RUN_TARGET,
        reason="first proof",
        active_stops=(_stop("effect-one", confirmed=True),),
    )
    assert first.status.state is ExecutionState.CANCEL_REQUESTED
    second = repository.cancel(
        cancel,
        RUN_TARGET,
        reason="second proof",
        active_stops=(_stop("run-owner:1", confirmed=True),),
    )
    assert second.status.state is ExecutionState.CANCELLED
    assert {item.operation_id for item in second.evidence} >= {
        "effect-one",
        "run-owner:1",
    }
    with psycopg.connect(database_url) as connection:
        effect_row = connection.execute(
            """SELECT state FROM router.effect_intents
               WHERE run_row_id = %s AND operation_identity = 'effect-one'""",
            (RUN_ROW_ID,),
        ).fetchone()
        lease_row = connection.execute(
            """SELECT expires_at <= transaction_timestamp()
               FROM router.run_leases WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        ).fetchone()
    assert effect_row is not None
    assert lease_row is not None
    effect_state = effect_row[0]
    lease_expired = lease_row[0]
    assert effect_state == "failed"
    assert lease_expired


def test_run_allows_only_one_active_business_effect(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Permit the next business effect only after the prior effect resolves."""
    _seed_running_run(database_url, repository, ("effect-one",))
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """INSERT INTO router.effect_intents (
                       id, run_row_id, owner_epoch, operation_identity, effect_kind,
                       request_fingerprint, state
                   ) VALUES (%s, %s, 1, 'effect-two', 'business-tool', %s, 'intent')""",
                (
                    "0198a080-0000-7000-8000-000000000070",
                    RUN_ROW_ID,
                    bytes.fromhex("0c" * 32),
                ),
            )
        connection.rollback()
        connection.execute(
            """UPDATE router.effect_intents
               SET state = 'confirmed', resolved_at = transaction_timestamp()
               WHERE run_row_id = %s AND operation_identity = 'effect-one'""",
            (RUN_ROW_ID,),
        )
        connection.execute(
            """INSERT INTO router.effect_intents (
                   id, run_row_id, owner_epoch, operation_identity, effect_kind,
                   request_fingerprint, state
               ) VALUES (%s, %s, 1, 'effect-two', 'business-tool', %s, 'intent')""",
            (
                "0198a080-0000-7000-8000-000000000070",
                RUN_ROW_ID,
                bytes.fromhex("0c" * 32),
            ),
        )


@pytest.mark.parametrize(("identity_length", "accepted"), [(500, True), (501, False)])
def test_business_effect_identity_matches_cancellation_evidence_limit(
    database_url: str,
    repository: PostgresExecutionRepository,
    identity_length: int,
    *,
    accepted: bool,
) -> None:
    """Accept the evidence limit and reject a longer operation identity."""
    _seed_running_run(database_url, repository, ())
    operation_identity = "x" * identity_length
    if accepted:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """INSERT INTO router.effect_intents (
                       id, run_row_id, owner_epoch, operation_identity, effect_kind,
                       request_fingerprint, state
                   ) VALUES (%s, %s, 1, %s, 'business-tool', %s, 'intent')""",
                (
                    "0198a080-0000-7000-8000-000000000077",
                    RUN_ROW_ID,
                    operation_identity,
                    bytes.fromhex("0f" * 32),
                ),
            )
        return
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.effect_intents (
                   id, run_row_id, owner_epoch, operation_identity, effect_kind,
                   request_fingerprint, state
               ) VALUES (%s, %s, 1, %s, 'business-tool', %s, 'intent')""",
            (
                "0198a080-0000-7000-8000-000000000077",
                RUN_ROW_ID,
                operation_identity,
                bytes.fromhex("0f" * 32),
            ),
        )


@pytest.mark.parametrize(
    ("start_state", "new_state"),
    [
        (ExecutionState.ADMITTED, ExecutionState.RUNNING),
        (ExecutionState.ADMITTED, ExecutionState.FAILED),
        (ExecutionState.RUNNING, ExecutionState.WAITING_FOR_TOOL),
        (ExecutionState.RUNNING, ExecutionState.SUCCEEDED),
        (ExecutionState.RUNNING, ExecutionState.FAILED),
        (ExecutionState.RUNNING, ExecutionState.INTERRUPTED),
        (ExecutionState.RUNNING, ExecutionState.UNCERTAIN),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.RUNNING),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.FAILED),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.UNCERTAIN),
    ],
)
def test_agent_run_allows_each_owner_fenced_non_cancel_edge(
    database_url: str,
    repository: PostgresExecutionRepository,
    start_state: ExecutionState,
    new_state: ExecutionState,
) -> None:
    """Journal each non-cancellation agent edge with the current owner."""
    if start_state is ExecutionState.ADMITTED:
        _seed_admitted_run(database_url)
        revision = 1
    else:
        _seed_running_run(database_url, repository, ())
        revision = 2
        if start_state is ExecutionState.WAITING_FOR_TOOL:
            repository.transition(
                _context("run.create", mutation=True),
                RUN_TARGET,
                expected_revision=revision,
                new_state=ExecutionState.WAITING_FOR_TOOL,
                owner_epoch=1,
                tool_call_id="prior-tool",
                tool_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            revision = 3
    tool_call_id = "next-tool" if new_state is ExecutionState.WAITING_FOR_TOOL else None
    tool_expires_at = (
        datetime.now(UTC) + timedelta(minutes=10)
        if new_state is ExecutionState.WAITING_FOR_TOOL
        else None
    )
    result = repository.transition(
        _context("run.create", mutation=True),
        RUN_TARGET,
        expected_revision=revision,
        new_state=new_state,
        owner_epoch=1,
        tool_call_id=tool_call_id,
        tool_expires_at=tool_expires_at,
    )
    assert result.state is new_state


@pytest.mark.parametrize(
    ("start_state", "new_state"),
    [
        (ExecutionState.ADMITTED, ExecutionState.ADMITTED),
        (ExecutionState.ADMITTED, ExecutionState.WAITING_FOR_TOOL),
        (ExecutionState.ADMITTED, ExecutionState.SUCCEEDED),
        (ExecutionState.ADMITTED, ExecutionState.INTERRUPTED),
        (ExecutionState.ADMITTED, ExecutionState.CANCELLED),
        (ExecutionState.ADMITTED, ExecutionState.UNCERTAIN),
        (ExecutionState.RUNNING, ExecutionState.ADMITTED),
        (ExecutionState.RUNNING, ExecutionState.RUNNING),
        (ExecutionState.RUNNING, ExecutionState.CANCELLED),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.ADMITTED),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.WAITING_FOR_TOOL),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.SUCCEEDED),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.INTERRUPTED),
        (ExecutionState.WAITING_FOR_TOOL, ExecutionState.CANCELLED),
    ],
)
def test_agent_run_rejects_each_forbidden_nonterminal_edge(
    database_url: str,
    repository: PostgresExecutionRepository,
    start_state: ExecutionState,
    new_state: ExecutionState,
) -> None:
    """Return the stable transition error for each forbidden agent edge."""
    if start_state is ExecutionState.ADMITTED:
        _seed_admitted_run(database_url)
        revision = 1
    else:
        _seed_running_run(database_url, repository, ())
        revision = 2
        if start_state is ExecutionState.WAITING_FOR_TOOL:
            repository.transition(
                _context("run.create", mutation=True),
                RUN_TARGET,
                expected_revision=revision,
                new_state=ExecutionState.WAITING_FOR_TOOL,
                owner_epoch=1,
                tool_call_id="prior-tool",
                tool_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            revision = 3
    with pytest.raises(ExecutionError) as error:
        repository.transition(
            _context("run.create", mutation=True),
            RUN_TARGET,
            expected_revision=revision,
            new_state=new_state,
            owner_epoch=1,
        )
    assert error.value.code is ExecutionErrorCode.INVALID_TRANSITION


def test_agent_run_rejects_stale_owner_transition_and_stream_append(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Fence a stale owner from both lifecycle and stream writes."""
    _seed_running_run(database_url, repository, ())
    write = _context("run.create", mutation=True)
    with pytest.raises(ExecutionError) as transition_error:
        repository.transition(
            write,
            RUN_TARGET,
            expected_revision=2,
            new_state=ExecutionState.WAITING_FOR_TOOL,
            owner_epoch=2,
            tool_call_id="tool-call",
            tool_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    assert transition_error.value.code is ExecutionErrorCode.OWNER_FENCED
    with pytest.raises(ExecutionError) as stream_error:
        repository.append_event(
            write,
            RUN_TARGET,
            event_name="usage.updated",
            payload={"usage": {"requests": 1}, "estimated": True},
            owner_epoch=2,
        )
    assert stream_error.value.code is ExecutionErrorCode.OWNER_FENCED


def test_cancellation_can_strengthen_unconfirmed_evidence(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Append a later confirmed result for one earlier unconfirmed operation."""
    _seed_running_run(database_url, repository, ("effect-one",))
    cancel = _context("run.cancel", mutation=True)
    first = repository.cancel(
        cancel,
        RUN_TARGET,
        reason="unconfirmed",
        active_stops=(_stop("effect-one", confirmed=False),),
    )
    assert first.status.state is ExecutionState.CANCEL_REQUESTED
    second = repository.cancel(
        cancel,
        RUN_TARGET,
        reason="confirmed",
        active_stops=(
            _stop("effect-one", confirmed=True),
            _stop("run-owner:1", confirmed=True),
        ),
    )
    assert second.status.state is ExecutionState.CANCELLED


def test_cancellation_reconciles_to_uncertain_at_database_deadline(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Use the durable ten-minute deadline for unproved active work."""
    _seed_running_run(database_url, repository, ("effect-one",))
    pending = repository.cancel(
        _context("run.cancel", mutation=True),
        RUN_TARGET,
        reason="cannot prove stop",
        active_stops=(_stop("effect-one", confirmed=False),),
    )
    assert pending.status.state is ExecutionState.CANCEL_REQUESTED
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """ALTER TABLE router.execution_cancellations
               DISABLE TRIGGER execution_cancellations_guard"""
        )
        connection.execute(
            """UPDATE router.execution_cancellations
               SET requested_at = transaction_timestamp() - interval '11 minutes',
                   reconcile_deadline = transaction_timestamp() - interval '1 minute'
               WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        )
        connection.execute(
            """ALTER TABLE router.execution_cancellations
               ENABLE TRIGGER execution_cancellations_guard"""
        )
        cancellation_row = connection.execute(
            """SELECT requested_at, reconcile_deadline
               FROM router.execution_cancellations WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        ).fetchone()
    assert cancellation_row is not None
    requested_at, deadline = cancellation_row
    assert deadline == requested_at + timedelta(minutes=10)
    result = repository.reconcile_cancellation(
        _system_context("run.reconcile"), RUN_TARGET
    )
    assert result.status.state is ExecutionState.UNCERTAIN
    assert result.status.safe_error is not None
    with psycopg.connect(database_url) as connection:
        effect_row = connection.execute(
            """SELECT state, resolved_at IS NOT NULL FROM router.effect_intents
               WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        ).fetchone()
        lease_row = connection.execute(
            """SELECT expires_at <= transaction_timestamp()
               FROM router.run_leases WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        ).fetchone()
    assert effect_row is not None
    assert lease_row is not None
    effect_state, resolved = effect_row
    lease_expired = lease_row[0]
    assert (effect_state, resolved) == ("uncertain", True)
    assert lease_expired


@pytest.mark.parametrize("with_effect", [False, True])
def test_run_takeover_resumes_only_without_unresolved_effect(
    database_url: str,
    repository: PostgresExecutionRepository,
    *,
    with_effect: bool,
) -> None:
    """Make an unresolved old-owner effect uncertain during fenced takeover."""
    operations = ("effect-one",) if with_effect else ()
    _seed_running_run(database_url, repository, operations)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.run_leases
               SET lease_generation = 2,
                   expires_at = transaction_timestamp() - interval '1 second'
               WHERE run_row_id = %s""",
            (RUN_ROW_ID,),
        )
    lease = repository.take_over_run(
        _system_context("run.takeover"),
        RUN_ID,
        owner_node_id="0198a080-0000-7000-8000-000000000063",
        control_epoch=1,
        expected_lease_generation=2,
        lease_duration=timedelta(minutes=5),
    )
    status = repository.status(_context("run.read", mutation=False), RUN_TARGET)
    if with_effect:
        assert status.state is ExecutionState.UNCERTAIN
        assert lease.lease_generation == 4  # noqa: PLR2004
        assert lease.expires_at <= datetime.now(UTC)
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT state FROM router.effect_intents WHERE run_row_id = %s",
                (RUN_ROW_ID,),
            ).fetchone() == ("uncertain",)
    else:
        assert status.state is ExecutionState.RUNNING
        assert lease.lease_generation == 3  # noqa: PLR2004
        assert lease.expires_at > datetime.now(UTC)


def test_agent_wait_expiry_is_bounded_and_canonical(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Accept a bounded wait and emit its UTC millisecond expiry."""
    _seed_running_run(database_url, repository, ())
    expiry = datetime.now(UTC) + timedelta(minutes=14)
    waiting = repository.transition(
        _context("run.create", mutation=True),
        RUN_TARGET,
        expected_revision=2,
        new_state=ExecutionState.WAITING_FOR_TOOL,
        owner_epoch=1,
        tool_call_id="tool-call",
        tool_expires_at=expiry,
    )
    assert waiting.state is ExecutionState.WAITING_FOR_TOOL
    event = repository.replay(
        _context("run.read", mutation=False), RUN_TARGET, after_sequence=2
    )[0]
    assert json.loads(event.wire_data)["payload"]["expires_at"].endswith("Z")


def test_agent_wait_expiry_rejects_more_than_fifteen_minutes(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject a wait expiry outside the database transition bound."""
    _seed_running_run(database_url, repository, ())
    with pytest.raises(ValueError, match="within 15 minutes"):
        repository.transition(
            _context("run.create", mutation=True),
            RUN_TARGET,
            expected_revision=2,
            new_state=ExecutionState.WAITING_FOR_TOOL,
            owner_epoch=1,
            tool_call_id="tool-call",
            tool_expires_at=datetime.now(UTC) + timedelta(minutes=16),
        )


def test_agent_wait_expiry_rejects_sub_millisecond_future(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject an expiry that truncates to the lifecycle event millisecond."""
    _seed_running_run(database_url, repository, ())
    with psycopg.connect(database_url) as connection:
        expiry = connection.execute(
            """SELECT date_trunc('milliseconds', clock_timestamp())
                      + interval '0.5 milliseconds'"""
        ).fetchone()
    assert expiry is not None
    with pytest.raises(ValueError, match="within 15 minutes"):
        repository.transition(
            _context("run.create", mutation=True),
            RUN_TARGET,
            expected_revision=2,
            new_state=ExecutionState.WAITING_FOR_TOOL,
            owner_epoch=1,
            tool_call_id="tool-call",
            tool_expires_at=expiry[0],
        )


def test_transition_replay_disconnect_and_commit_boundaries(  # noqa: D103
    repository: PostgresExecutionRepository,
) -> None:
    write = _context("model.create", mutation=True)
    read = _context("model.read", mutation=False)

    running = repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    delta = repository.append_event(
        write,
        TARGET,
        event_name="output.delta",
        payload={"output_index": 0, "content_type": "text/plain", "delta": "ok"},
        expected_sequence=3,
    )
    assert running.state_revision == 2  # noqa: PLR2004
    assert delta.sequence == 3  # noqa: PLR2004
    disconnected = repository.stream_disconnected(read, TARGET)
    assert disconnected.state is ExecutionState.RUNNING
    assert disconnected.partial_output
    assert not disconnected.fallback_permitted
    assert [
        event.sequence for event in repository.replay(read, TARGET, after_sequence=1)
    ] == [2, 3]

    terminal = repository.transition(
        write, TARGET, expected_revision=2, new_state=ExecutionState.SUCCEEDED
    )
    assert terminal.terminal_at is not None
    assert terminal.terminal
    assert terminal.accepts_late_usage
    assert terminal.state_revision == 3  # noqa: PLR2004
    assert (
        repository.replay(read, TARGET, after_sequence=3)[0].event_name
        == "request.terminal"
    )
    with pytest.raises(ExecutionError) as error:
        repository.transition(
            write, TARGET, expected_revision=3, new_state=ExecutionState.FAILED
        )
    assert error.value.code is ExecutionErrorCode.INVALID_TRANSITION


def test_replay_is_scope_isolated_tamper_protected_and_expires(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Keep exact stream bytes in scope and stop replay after retention."""
    write = _context("model.create", mutation=True)
    read = _context("model.read", mutation=False)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    terminal = repository.transition(
        write, TARGET, expected_revision=2, new_state=ExecutionState.SUCCEEDED
    )
    assert terminal.terminal_at is not None
    events = repository.replay(read, TARGET, after_sequence=0)
    assert len(events) == 3  # noqa: PLR2004
    with psycopg.connect(database_url) as connection:
        expiries = connection.execute(
            """SELECT DISTINCT expires_at FROM router.execution_stream_events
               WHERE request_row_id = %s""",
            (REQUEST_ROW_ID,),
        ).fetchall()
    assert expiries == [(terminal.terminal_at + timedelta(minutes=15),)]

    outside_scope = _context(
        "model.read",
        mutation=False,
        service_id=OTHER_SERVICE_ID,
        workspace_id=OTHER_WORKSPACE_ID,
    )
    with pytest.raises(ExecutionError) as hidden:
        repository.replay(outside_scope, TARGET, after_sequence=0)
    assert hidden.value.code is ExecutionErrorCode.NOT_FOUND

    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.execution_stream_events
               SET wire_data = wire_data || ' '
               WHERE request_row_id = %s AND sequence = 1""",
            (REQUEST_ROW_ID,),
        )
    original = events[0].wire_data
    tampered_envelope = json.loads(original)
    tampered_envelope["request_id"] = "0198a080-0000-7000-8000-000000000099"
    tampered = json.dumps(tampered_envelope, separators=(",", ":"), sort_keys=True)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               DISABLE TRIGGER execution_stream_events_append_only"""
        )
        connection.execute(
            """UPDATE router.execution_stream_events
               SET wire_data = %s, wire_sha256 = %s
               WHERE request_row_id = %s AND sequence = 1""",
            (tampered, hashlib.sha256(tampered.encode()).digest(), REQUEST_ROW_ID),
        )
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               ENABLE TRIGGER execution_stream_events_append_only"""
        )
    with pytest.raises(ExecutionError) as corrupted:
        repository.replay(read, TARGET, after_sequence=0)
    assert corrupted.value.code is ExecutionErrorCode.STREAM_CONFLICT
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               DISABLE TRIGGER execution_stream_events_append_only"""
        )
        connection.execute(
            """UPDATE router.execution_stream_events
               SET wire_data = %s, wire_sha256 = %s
               WHERE request_row_id = %s AND sequence = 1""",
            (original, hashlib.sha256(original.encode()).digest(), REQUEST_ROW_ID),
        )
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               ENABLE TRIGGER execution_stream_events_append_only"""
        )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               DISABLE TRIGGER execution_stream_events_append_only"""
        )
        connection.execute(
            """UPDATE router.execution_stream_events
               SET expires_at = transaction_timestamp() - interval '1 second'
               WHERE request_row_id = %s""",
            (REQUEST_ROW_ID,),
        )
        connection.execute(
            """ALTER TABLE router.execution_stream_events
               ENABLE TRIGGER execution_stream_events_append_only"""
        )
    with pytest.raises(ExecutionError) as expired:
        repository.replay(read, TARGET, after_sequence=0)
    assert expired.value.code is ExecutionErrorCode.STREAM_REPLAY_UNAVAILABLE


def test_business_effect_stops_fallback_and_cancellation_stops_new_effects(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Commit one business effect and stop new work after cancellation."""
    _seed_running_run(database_url, repository, ())
    write = _context("run.create", mutation=True)
    repository.append_event(
        write,
        RUN_TARGET,
        event_name="tool.started",
        payload={"tool_call_id": "business-one", "tool_kind": "business"},
        owner_epoch=1,
    )
    status = repository.status(_context("run.read", mutation=False), RUN_TARGET)
    assert status.committed_effects
    assert not status.fallback_permitted

    pending = repository.cancel(
        _context("run.cancel", mutation=True),
        RUN_TARGET,
        reason="stop the run",
        active_stops=(_stop("run-owner:1", confirmed=False),),
    )
    assert pending.status.state is ExecutionState.CANCEL_REQUESTED
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.effect_intents (
                   id, run_row_id, owner_epoch, operation_identity, effect_kind,
                   request_fingerprint, state
               ) VALUES (%s, %s, 1, 'late-effect', 'business-tool', %s, 'intent')""",
            (
                "0198a080-0000-7000-8000-000000000071",
                RUN_ROW_ID,
                bytes.fromhex("0d" * 32),
            ),
        )


def test_provider_attempt_needs_running_journal_and_stops_after_output_commit(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Require the running event and stop fallback attempts after output."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.route_price_versions (
                   id, provider_model_route_id, version_number, currency, status
               ) VALUES (%s, %s, 1, 'USD', 'current')""",
            (PRICE_VERSION_ID, FIXTURE_ROUTE_ID),
        )
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="running transition"),
        psycopg.connect(database_url) as connection,
    ):
        _insert_provider_attempt(
            connection,
            attempt_id="0198a080-0000-7000-8000-000000000073",
            attempt_number=1,
        )

    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    with psycopg.connect(database_url) as connection:
        _insert_provider_attempt(
            connection,
            attempt_id="0198a080-0000-7000-8000-000000000074",
            attempt_number=1,
        )
    repository.append_event(
        write,
        TARGET,
        event_name="output.delta",
        payload={"output_index": 0, "content_type": "text/plain", "delta": "x"},
    )
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        _insert_provider_attempt(
            connection,
            attempt_id="0198a080-0000-7000-8000-000000000075",
            attempt_number=2,
        )


@pytest.mark.parametrize(
    "new_state",
    [
        ExecutionState.WAITING_FOR_TOOL,
        ExecutionState.SUCCEEDED,
        ExecutionState.INTERRUPTED,
        ExecutionState.CANCELLED,
        ExecutionState.UNCERTAIN,
    ],
)
def test_logical_admission_rejects_every_forbidden_edge(
    repository: PostgresExecutionRepository, new_state: ExecutionState
) -> None:
    """Return one stable error for each forbidden logical admission edge."""
    with pytest.raises(ExecutionError) as error:
        repository.transition(
            _context("model.create", mutation=True),
            TARGET,
            expected_revision=1,
            new_state=new_state,
        )
    assert error.value.code is ExecutionErrorCode.INVALID_TRANSITION


@pytest.mark.parametrize(
    "terminal_state",
    [ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.INTERRUPTED],
)
def test_logical_running_allows_each_direct_terminal_edge(
    repository: PostgresExecutionRepository, terminal_state: ExecutionState
) -> None:
    """Journal every allowed direct logical terminal edge from running."""
    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    status = repository.transition(
        write, TARGET, expected_revision=2, new_state=terminal_state
    )
    assert status.state is terminal_state
    assert status.admission.state is ExecutionState.ADMITTED
    assert status.admission.state_revision == 1


def test_revision_and_stream_duplicates_are_exact(  # noqa: D103
    repository: PostgresExecutionRepository,
) -> None:
    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    first = repository.append_event(
        write,
        TARGET,
        event_name="usage.updated",
        payload={"usage": {"input": 1}, "estimated": True},
        expected_sequence=3,
    )
    duplicate = repository.append_event(
        write,
        TARGET,
        event_name="usage.updated",
        payload={"usage": {"input": 1}, "estimated": True},
        expected_sequence=3,
    )
    assert duplicate.wire_data == first.wire_data

    with pytest.raises(ExecutionError) as stream_error:
        repository.append_event(
            write,
            TARGET,
            event_name="usage.updated",
            payload={"usage": {"input": 2}, "estimated": True},
            expected_sequence=3,
        )
    assert stream_error.value.code is ExecutionErrorCode.STREAM_CONFLICT
    with pytest.raises(ExecutionError) as revision_error:
        repository.transition(
            write, TARGET, expected_revision=1, new_state=ExecutionState.SUCCEEDED
        )
    assert revision_error.value.code is ExecutionErrorCode.REVISION_CONFLICT


def test_cancel_intent_is_durable_before_stop_and_retries_are_idempotent(  # noqa: D103
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    write = _context("model.create", mutation=True)
    cancel = _context("model.cancel", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    observations: list[tuple[str, int]] = []

    def stop() -> AdapterStopEvidence:
        with psycopg.connect(database_url) as connection:
            observation = connection.execute(
                """SELECT request.state, count(cancellation.request_row_id)
                   FROM router.logical_requests AS request
                   LEFT JOIN router.execution_cancellations AS cancellation
                     ON cancellation.request_row_id = request.row_id
                   WHERE request.row_id = %s GROUP BY request.state""",
                (REQUEST_ROW_ID,),
            ).fetchone()
        assert observation is not None
        state, count = observation
        observations.append((state, count))
        return AdapterStopEvidence("not-active", True, True, False)  # noqa: FBT003

    pending = repository.cancel(
        cancel, TARGET, reason="caller cancelled", active_stops=(stop,)
    )
    assert observations == [("cancel_requested", 1)]
    assert pending.status.state is ExecutionState.CANCELLED
    assert pending.reconcile_deadline is not None

    retry = repository.cancel(cancel, TARGET, reason="retry", active_stops=())
    assert retry.status.state is ExecutionState.CANCELLED
    assert retry.reconcile_deadline == pending.reconcile_deadline
    repeated = repository.cancel(cancel, TARGET, reason="repeat", active_stops=())
    assert repeated.status.state is ExecutionState.CANCELLED
    assert repeated.reconcile_deadline == pending.reconcile_deadline
    with psycopg.connect(database_url) as connection:
        cancellation_row = connection.execute(
            "SELECT count(*) FROM router.execution_cancellations WHERE request_row_id = %s",  # noqa: E501
            (REQUEST_ROW_ID,),
        ).fetchone()
        audit_row = connection.execute(
            "SELECT count(*) FROM router.execution_cancellation_audit WHERE request_row_id = %s",  # noqa: E501
            (REQUEST_ROW_ID,),
        ).fetchone()
    assert cancellation_row is not None
    assert audit_row is not None
    cancellation_count = cancellation_row[0]
    audit_count = audit_row[0]
    assert cancellation_count == 1
    assert audit_count >= 4  # noqa: PLR2004


def test_cancellation_recomputes_active_work_after_adapter_callback(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Finish cancellation when active work ends during the stop callback."""
    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.route_price_versions (
                   id, provider_model_route_id, version_number, currency, status
               ) VALUES (%s, %s, 1, 'USD', 'current')""",
            (PRICE_VERSION_ID, FIXTURE_ROUTE_ID),
        )
        _insert_provider_attempt(
            connection,
            attempt_id="0198a080-0000-7000-8000-000000000076",
            attempt_number=1,
        )

    def stop() -> AdapterStopEvidence:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """UPDATE router.provider_attempts
                   SET state = 'failed', finished_at = transaction_timestamp()
                   WHERE request_row_id = %s AND state = 'started'""",
                (REQUEST_ROW_ID,),
            )
        return AdapterStopEvidence(
            operation_id="attempt-ended",
            supported=True,
            stop_requested=True,
            confirmed_stopped=False,
        )

    result = repository.cancel(
        _context("model.cancel", mutation=True),
        TARGET,
        reason="adapter ended",
        active_stops=(stop,),
    )
    assert result.status.state is ExecutionState.CANCELLED


def test_cancellation_audits_not_found_denied_and_terminal_results(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Write safe audit rows for each cancellation decision result."""
    missing_target = ExecutionTarget(
        ExecutionKind.MODEL, "0198a080-0000-7000-8000-000000000099"
    )
    cancel = _context("model.cancel", mutation=True)
    with pytest.raises(ExecutionError) as missing:
        repository.cancel(cancel, missing_target, reason="missing")
    assert missing.value.code is ExecutionErrorCode.NOT_FOUND

    with pytest.raises(ExecutionError) as denied:
        repository.cancel(
            _context("model.read", mutation=True), TARGET, reason="denied"
        )
    assert denied.value.code is ExecutionErrorCode.INSUFFICIENT_SCOPE

    cross_service_workspace = _context(
        "model.cancel",
        mutation=True,
        service_id=SERVICE_ID,
        workspace_id=OTHER_WORKSPACE_ID,
    )
    with pytest.raises(ExecutionError) as cross_service:
        repository.cancel(
            cross_service_workspace, TARGET, reason="cross-service workspace"
        )
    assert cross_service.value.code is ExecutionErrorCode.NOT_FOUND

    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    repository.transition(
        write, TARGET, expected_revision=2, new_state=ExecutionState.SUCCEEDED
    )
    too_late = repository.cancel(cancel, TARGET, reason="too late")
    assert too_late.too_late
    assert too_late.status.state is ExecutionState.SUCCEEDED

    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """SELECT target_public_id::text, permission_result, final_result,
                      request_row_id
               FROM router.execution_cancellation_audit
               ORDER BY occurred_at, event_id"""
        ).fetchall()
    assert (
        missing_target.public_id,
        "denied",
        "denied",
        None,
    ) in rows
    assert (TARGET.public_id, "denied", "denied", None) in rows
    assert (
        TARGET.public_id,
        "allowed",
        "too_late",
        uuid.UUID(REQUEST_ROW_ID),
    ) in rows
    with psycopg.connect(database_url) as connection:
        attempted_scope = connection.execute(
            """SELECT service_id, workspace_id
               FROM router.execution_cancellation_audit
               WHERE target_public_id = %s AND permission_result = 'denied'
                 AND workspace_id = %s""",
            (TARGET.public_id, OTHER_WORKSPACE_ID),
        ).fetchall()
    assert attempted_scope == [(uuid.UUID(SERVICE_ID), uuid.UUID(OTHER_WORKSPACE_ID))]
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.execution_cancellation_audit
               SET final_result = 'accepted'
               WHERE target_public_id = %s AND permission_result = 'denied'
                 AND workspace_id = %s""",
            (TARGET.public_id, OTHER_WORKSPACE_ID),
        )


def test_concurrent_cancel_callbacks_return_one_idempotent_result(
    repository: PostgresExecutionRepository,
) -> None:
    """Return the durable terminal result to both second-phase cancel callers."""
    repository.transition(
        _context("model.create", mutation=True),
        TARGET,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    barrier = threading.Barrier(2)

    def stop() -> AdapterStopEvidence:
        barrier.wait(timeout=5)
        return AdapterStopEvidence(
            operation_id="not-active",
            supported=True,
            stop_requested=True,
            confirmed_stopped=False,
        )

    def cancel() -> ExecutionState:
        return repository.cancel(
            _context("model.cancel", mutation=True),
            TARGET,
            reason="concurrent",
            active_stops=(stop,),
        ).status.state

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: cancel(), range(2)))
    assert results == [ExecutionState.CANCELLED, ExecutionState.CANCELLED]


def test_execution_insert_cannot_bypass_clean_admission_state(  # noqa: D103
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    del repository
    with psycopg.connect(database_url) as connection:  # noqa: SIM117
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.logical_requests (
                       row_id, request_id, request_kind, service_id, workspace_id,
                       configuration_revision_id, fingerprint_version,
                       fingerprint_sha256, data_profile, capture_enabled,
                       state, state_revision, partial_output
                   ) VALUES (%s, %s, 'shared_tool', %s, %s, %s, 1, %s,
                             'service-data', true, 'running', 2, true)""",
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    SERVICE_ID,
                    WORKSPACE_ID,
                    "0198a080-0000-7000-8000-000000000004",
                    bytes(32),
                ),
            )


def _insert_direct_event(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    sequence: int,
    event_name: str,
    payload: dict[str, object],
) -> datetime:
    timing = connection.execute(
        "SELECT date_trunc('milliseconds', transaction_timestamp())"
    ).fetchone()
    assert timing is not None
    occurred_at = timing[0]
    assert isinstance(occurred_at, datetime)
    event = make_event(
        TARGET,
        sequence=sequence,
        event_name=event_name,
        occurred_at=occurred_at,
        payload=payload,
    )
    connection.execute(
        """INSERT INTO router.execution_stream_events (
               request_row_id, service_id, workspace_id, sequence, event_name,
               occurred_at, wire_data, wire_sha256
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            REQUEST_ROW_ID,
            SERVICE_ID,
            WORKSPACE_ID,
            sequence,
            event_name,
            occurred_at,
            event.wire_data,
            hashlib.sha256(event.wire_data.encode()).digest(),
        ),
    )
    return occurred_at


def test_cancel_state_cannot_bypass_intent_and_audit(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Route cancellation through the dedicated durable operation."""
    with pytest.raises(ExecutionError) as api_error:
        repository.transition(
            _context("model.create", mutation=True),
            TARGET,
            expected_revision=1,
            new_state=ExecutionState.CANCEL_REQUESTED,
        )
    assert api_error.value.code is ExecutionErrorCode.INVALID_TRANSITION

    with (  # noqa: PT012 -- The deferred constraint checks the full transaction.
        pytest.raises(psycopg.errors.CheckViolation, match="cancellation intent"),
        psycopg.connect(database_url) as connection,
    ):
        transition_time = _insert_direct_event(
            connection,
            sequence=2,
            event_name="request.cancel_requested",
            payload={"state_revision": 2},
        )
        connection.execute(
            """UPDATE router.logical_requests
               SET state = 'cancel_requested', state_revision = 2,
                   last_transition_at = %s
               WHERE row_id = %s""",
            (transition_time, REQUEST_ROW_ID),
        )

    with (  # noqa: PT012 -- The deferred constraint checks the full transaction.
        pytest.raises(psycopg.errors.CheckViolation, match="cancellation audit"),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.execution_cancellations (
                   request_row_id, service_id, workspace_id, actor_kind, actor_id,
                   prior_state, reason_sha256, requested_at, reconcile_deadline
               ) VALUES (%s, %s, %s, 'service', %s, 'admitted', %s,
                         transaction_timestamp(),
                         transaction_timestamp() + interval '10 minutes')""",
            (
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                SERVICE_ID,
                bytes.fromhex("0e" * 32),
            ),
        )
        transition_time = _insert_direct_event(
            connection,
            sequence=2,
            event_name="request.cancel_requested",
            payload={"state_revision": 2},
        )
        connection.execute(
            """UPDATE router.logical_requests
               SET state = 'cancel_requested', state_revision = 2,
                   last_transition_at = %s
               WHERE row_id = %s""",
            (transition_time, REQUEST_ROW_ID),
        )


def test_later_extension_cannot_hide_unapplied_state_event(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Validate every state event even when a later extension event exists."""
    del repository
    with (  # noqa: PT012
        pytest.raises(
            psycopg.errors.CheckViolation, match=r"not applied|does not match"
        ),
        psycopg.connect(database_url) as connection,
    ):
        _insert_direct_event(
            connection,
            sequence=2,
            event_name="request.running",
            payload={"state_revision": 2},
        )
        _insert_direct_event(
            connection,
            sequence=3,
            event_name="extension.after-state",
            payload={},
        )


def test_later_extension_cannot_hide_unapplied_output_commit(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Validate every output commit event even when an extension follows it."""
    repository.transition(
        _context("model.create", mutation=True),
        TARGET,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    with (  # noqa: PT012
        pytest.raises(psycopg.errors.CheckViolation, match="output event"),
        psycopg.connect(database_url) as connection,
    ):
        _insert_direct_event(
            connection,
            sequence=3,
            event_name="output.delta",
            payload={"output_index": 0, "content_type": "text/plain", "delta": "x"},
        )
        _insert_direct_event(
            connection,
            sequence=4,
            event_name="extension.after-output",
            payload={},
        )


def test_stream_envelope_rejects_null_required_values(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject an exact-key envelope when required values are JSON null."""
    del repository
    wire_data = json.dumps(
        {
            "stream_version": None,
            "request_id": None,
            "sequence": None,
            "occurred_at": None,
            "payload": {},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="wire envelope"),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.execution_stream_events (
                   request_row_id, service_id, workspace_id, sequence, event_name,
                   occurred_at, wire_data, wire_sha256
               ) VALUES (%s, %s, %s, 2, 'extension.nulls', %s, %s, %s)""",
            (
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                NOW,
                wire_data,
                hashlib.sha256(wire_data.encode()).digest(),
            ),
        )


def test_database_rejects_malformed_core_stream_payload(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Keep the durable journal valid when a writer bypasses the repository."""
    repository.transition(
        _context("model.create", mutation=True),
        TARGET,
        expected_revision=1,
        new_state=ExecutionState.RUNNING,
    )
    with psycopg.connect(database_url) as connection:
        occurred_at = connection.execute(
            "SELECT date_trunc('milliseconds', transaction_timestamp())"
        ).fetchone()
        assert occurred_at is not None
        wire_data = json.dumps(
            {
                "stream_version": "1",
                "request_id": TARGET.public_id,
                "sequence": 3,
                "occurred_at": occurred_at[0]
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "payload": {"output_index": 0, "content_type": "text/plain"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO router.execution_stream_events (
                       request_row_id, service_id, workspace_id, sequence, event_name,
                       occurred_at, wire_data, wire_sha256
                   ) VALUES (%s, %s, %s, 3, 'output.delta', %s, %s, %s)""",
                (
                    REQUEST_ROW_ID,
                    SERVICE_ID,
                    WORKSPACE_ID,
                    occurred_at[0],
                    wire_data,
                    hashlib.sha256(wire_data.encode()).digest(),
                ),
            )


def test_database_rejects_coerced_cancellation_proof(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Do not accept text values as proof that active work stopped."""
    del repository
    evidence = [
        {
            "operation_id": "attempt-one",
            "supported": True,
            "stop_requested": True,
            "confirmed_stopped": "true",
            "safe_code": None,
        }
    ]
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.execution_cancellations (
                   request_row_id, service_id, workspace_id, actor_kind, actor_id,
                   prior_state, reason_sha256, requested_at, reconcile_deadline,
                   adapter_stop_evidence
               ) VALUES (%s, %s, %s, 'service', %s, 'admitted', %s,
                         transaction_timestamp(),
                         transaction_timestamp() + interval '10 minutes', %s::jsonb)""",
            (
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                SERVICE_ID,
                bytes.fromhex("0f" * 32),
                json.dumps(evidence),
            ),
        )


def test_lifecycle_events_cannot_reuse_one_state_revision(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject two lifecycle events that claim the same state revision."""
    del repository
    with (  # noqa: PT012
        pytest.raises(psycopg.errors.SerializationFailure, match="revision"),
        psycopg.connect(database_url) as connection,
    ):
        _insert_direct_event(
            connection,
            sequence=2,
            event_name="request.running",
            payload={"state_revision": 2},
        )
        _insert_direct_event(
            connection,
            sequence=3,
            event_name="request.terminal",
            payload={
                "state": "failed",
                "state_revision": 2,
                "partial_output": False,
                "committed_effects": False,
            },
        )


def test_terminal_event_error_must_match_status_error(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject different safe errors in the terminal event and status row."""
    del repository
    with (  # noqa: PT012
        pytest.raises(psycopg.errors.CheckViolation, match="terminal event"),
        psycopg.connect(database_url) as connection,
    ):
        timing = connection.execute(
            """SELECT transaction_timestamp(),
                      date_trunc('milliseconds', transaction_timestamp())"""
        ).fetchone()
        assert timing is not None
        transition_time, event_time = timing
        event = make_event(
            TARGET,
            sequence=2,
            event_name="request.terminal",
            occurred_at=event_time,
            payload={
                "state": "failed",
                "state_revision": 2,
                "partial_output": False,
                "committed_effects": False,
                "error": {
                    "class": "timeout",
                    "affected_scope": "attempt",
                    "message": "event error",
                },
            },
            expires_at=transition_time + timedelta(minutes=15),
        )
        connection.execute(
            """INSERT INTO router.execution_stream_events (
                   request_row_id, service_id, workspace_id, sequence, event_name,
                   occurred_at, wire_data, wire_sha256, expires_at
               ) VALUES (%s, %s, %s, 2, 'request.terminal', %s, %s, %s, %s)""",
            (
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                event_time,
                event.wire_data,
                hashlib.sha256(event.wire_data.encode()).digest(),
                transition_time + timedelta(minutes=15),
            ),
        )
        connection.execute(
            """UPDATE router.execution_stream_events SET expires_at = %s
               WHERE request_row_id = %s AND expires_at IS NULL""",
            (transition_time + timedelta(minutes=15), REQUEST_ROW_ID),
        )
        connection.execute(
            """UPDATE router.logical_requests
               SET state = 'failed', state_revision = 2,
                   last_transition_at = %s, terminal_at = %s,
                   expires_at = %s,
                   safe_error = %s::jsonb
               WHERE row_id = %s""",
            (
                transition_time,
                transition_time,
                transition_time + timedelta(hours=24),
                json.dumps(
                    {
                        "class": "transport",
                        "affected_scope": "attempt",
                        "message": "status error",
                    }
                ),
                REQUEST_ROW_ID,
            ),
        )


def test_reached_terminal_state_and_direct_metadata_are_immutable(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject direct safe-error, replay-expiry, time, and terminal changes."""
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="safe error"),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.logical_requests
               SET safe_error = '{"class":"timeout"}'::jsonb
               WHERE row_id = %s""",
            (REQUEST_ROW_ID,),
        )
    with (
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.execution_stream_events
               SET expires_at = transaction_timestamp() + interval '15 minutes'
               WHERE request_row_id = %s AND sequence = 1""",
            (REQUEST_ROW_ID,),
        )
    with (
        pytest.raises(psycopg.errors.SerializationFailure),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """UPDATE router.logical_requests
               SET last_transition_at = last_transition_at - interval '1 second'
               WHERE row_id = %s""",
            (REQUEST_ROW_ID,),
        )

    write = _context("model.create", mutation=True)
    repository.transition(
        write, TARGET, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    terminal = repository.transition(
        write, TARGET, expected_revision=2, new_state=ExecutionState.SUCCEEDED
    )
    assert terminal.terminal_at is not None
    safe_error_mutation = (
        "safe_error = '"
        + json.dumps(
            {
                "class": "timeout",
                "affected_scope": "attempt",
                "message": "late",
            }
        )
        + "'::jsonb"
    )
    mutations = (
        "state = 'failed'",
        "terminal_at = terminal_at + interval '1 second'",
        "expires_at = expires_at + interval '1 second'",
        safe_error_mutation,
    )
    for mutation in mutations:
        with (
            pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
            psycopg.connect(database_url) as connection,
        ):
            connection.execute(
                f"UPDATE router.logical_requests SET {mutation} WHERE row_id = %s",  # noqa: S608
                (REQUEST_ROW_ID,),
            )


def test_runtime_lifecycle_event_cannot_be_backdated(
    database_url: str, repository: PostgresExecutionRepository
) -> None:
    """Reject a runtime lifecycle event that does not use database time."""
    del repository
    occurred_at = NOW - timedelta(days=1)
    event = make_event(
        TARGET,
        sequence=2,
        event_name="request.running",
        occurred_at=occurred_at,
        payload={"state_revision": 2},
    )
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="database time"),
        psycopg.connect(database_url) as connection,
    ):
        connection.execute(
            """INSERT INTO router.execution_stream_events (
                   request_row_id, service_id, workspace_id, sequence, event_name,
                   occurred_at, wire_data, wire_sha256
               ) VALUES (%s, %s, %s, 2, 'request.running', %s, %s, %s)""",
            (
                REQUEST_ROW_ID,
                SERVICE_ID,
                WORKSPACE_ID,
                occurred_at,
                event.wire_data,
                hashlib.sha256(event.wire_data.encode()).digest(),
            ),
        )

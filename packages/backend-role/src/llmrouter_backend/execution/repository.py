"""PostgreSQL execution lifecycle, stream, cancellation, and recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
)

from .errors import ExecutionError, ExecutionErrorCode
from .model import (
    CANCELLATION_RECONCILIATION_SECONDS,
    STREAM_REPLAY_AFTER_TERMINAL_SECONDS,
    TERMINAL_STATES,
    AdapterStop,
    AdapterStopEvidence,
    CancellationResult,
    ErrorScope,
    ExecutionAdmission,
    ExecutionKind,
    ExecutionState,
    ExecutionStatus,
    ExecutionTarget,
    RunLease,
    TerminalError,
    TerminalErrorClass,
)
from .stream import StreamEvent, make_event

CANCELLATION_REASON_MAXIMUM_CHARACTERS = 500
MAXIMUM_TOOL_WAIT_SECONDS = 900
_LOGICAL_TRANSITIONS = {
    ExecutionState.ADMITTED: frozenset(
        {ExecutionState.RUNNING, ExecutionState.CANCEL_REQUESTED, ExecutionState.FAILED}
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.INTERRUPTED,
            ExecutionState.CANCEL_REQUESTED,
        }
    ),
    ExecutionState.CANCEL_REQUESTED: frozenset(
        {ExecutionState.CANCELLED, ExecutionState.UNCERTAIN}
    ),
}
_RUN_TRANSITIONS = {
    **_LOGICAL_TRANSITIONS,
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.WAITING_FOR_TOOL,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.INTERRUPTED,
            ExecutionState.CANCEL_REQUESTED,
            ExecutionState.UNCERTAIN,
        }
    ),
    ExecutionState.WAITING_FOR_TOOL: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.FAILED,
            ExecutionState.CANCEL_REQUESTED,
            ExecutionState.UNCERTAIN,
        }
    ),
}

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from psycopg import Connection

    from llmrouter_backend.authority import RequestContext


class PostgresExecutionRepository:
    """Keep one execution lifecycle durable and scoped across nodes."""

    def __init__(self, database_url: str) -> None:
        """Set the PostgreSQL connection address."""
        if not database_url:
            msg = "The database URL must not be empty."
            raise ValueError(msg)
        self._database_url = database_url

    def status(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ExecutionStatus:
        """Read lifecycle state only in the exact original scope."""
        _require_read_authority(context, target)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = _select_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            return _status(connection, target, row)

    def transition(  # noqa: PLR0913 -- Public transition contract fields are explicit.
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        expected_revision: int,
        new_state: ExecutionState,
        safe_error: TerminalError | None = None,
        owner_epoch: int | None = None,
        tool_call_id: str | None = None,
        tool_expires_at: datetime | None = None,
    ) -> ExecutionStatus:
        """Commit one state transition and its exact stream event together."""
        _require_internal_authority(context, target)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = _lock_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            if int(row["state_revision"]) != expected_revision:
                raise ExecutionError(
                    ExecutionErrorCode.REVISION_CONFLICT, context.request_id
                )
            if (
                new_state
                in {
                    ExecutionState.CANCEL_REQUESTED,
                    ExecutionState.CANCELLED,
                }
                or ExecutionState(row["state"]) is ExecutionState.CANCEL_REQUESTED
            ):
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_TRANSITION, context.request_id
                )
            updated = _transition_locked(
                connection,
                target,
                row,
                new_state,
                safe_error=safe_error,
                owner_epoch=owner_epoch,
                tool_call_id=tool_call_id,
                tool_expires_at=tool_expires_at,
            )
            return _status(connection, target, updated)

    def append_event(  # noqa: PLR0913 -- Public stream contract fields are explicit.
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        event_name: str,
        payload: dict[str, object],
        expected_sequence: int | None = None,
        owner_epoch: int | None = None,
    ) -> StreamEvent:
        """Append one event and commit its visible-output or effect boundary."""
        _require_internal_authority(context, target)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = _lock_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            if event_name.startswith("request."):
                raise ExecutionError(
                    ExecutionErrorCode.STREAM_CONFLICT, context.request_id
                )
            if row["state"] not in {"running", "waiting_for_tool"}:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_TRANSITION, context.request_id
                )
            if target.kind is ExecutionKind.AGENT_RUN:
                live_owner = _current_owner_epoch(connection, target, row)
                if owner_epoch is None or owner_epoch != live_owner:
                    raise ExecutionError(
                        ExecutionErrorCode.OWNER_FENCED, context.request_id
                    )
            event = _append_locked(
                connection,
                target,
                row,
                event_name,
                payload,
                expected_sequence=expected_sequence,
                owner_epoch=owner_epoch,
            )
            if event_name == "output.delta" and not row["partial_output"]:
                _set_commit_indicators(
                    connection, target, row["row_id"], partial_output=True
                )
            if (
                event_name == "tool.started"
                and payload.get("tool_kind") == "business"
                and not row["committed_effect"]
            ):
                _set_commit_indicators(
                    connection, target, row["row_id"], committed_effect=True
                )
            return event

    def replay(
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        after_sequence: int,
    ) -> tuple[StreamEvent, ...]:
        """Return retained events after one cursor without changing content."""
        _require_read_authority(context, target)
        if target.kind is ExecutionKind.SHARED_TOOL:
            raise ExecutionError(
                ExecutionErrorCode.STREAM_REPLAY_UNAVAILABLE, context.request_id
            )
        if after_sequence < 0:
            msg = "A replay cursor cannot be negative."
            raise ValueError(msg)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = _select_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            target_columns = _target_columns(target)
            bounds = connection.execute(
                f"""SELECT min(sequence) AS minimum, max(sequence) AS maximum,
                           bool_or(expires_at IS NOT NULL
                                   AND expires_at <= transaction_timestamp()) AS expired
                    FROM router.execution_stream_events
                    WHERE {target_columns[0]} = %s""",  # noqa: S608  # nosec B608
                (row["row_id"],),
            ).fetchone()
            if bounds is None:
                message = "A stream bounds query did not return a row."
                raise RuntimeError(message)
            if (
                bounds["minimum"] is None
                or bounds["expired"]
                or (after_sequence + 1 < int(bounds["minimum"]))
            ):
                raise ExecutionError(
                    ExecutionErrorCode.STREAM_REPLAY_UNAVAILABLE, context.request_id
                )
            events = connection.execute(
                f"""SELECT * FROM router.execution_stream_events
                    WHERE {target_columns[0]} = %s AND sequence > %s
                    ORDER BY sequence""",  # noqa: S608  # nosec B608
                (row["row_id"], after_sequence),
            ).fetchall()
            return tuple(_stored_event(target, event) for event in events)

    def stream_disconnected(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ExecutionStatus:
        """Return status without treating a transport close as cancellation."""
        return self.status(context, target)

    def cancel(  # noqa: C901, PLR0915
        self,
        context: RequestContext,
        target: ExecutionTarget,
        *,
        reason: str,
        active_stops: Sequence[AdapterStop] = (),
    ) -> CancellationResult:
        """Record cancel intent before adapter stop calls and reconcile evidence."""
        if not 1 <= len(reason) <= CANCELLATION_REASON_MAXIMUM_CHARACTERS:
            msg = "A cancellation reason must contain 1 through 500 characters."
            raise ValueError(msg)
        try:
            _require_cancel_authority(context, target)
        except ExecutionError:
            self._audit_denied(context, target)
            raise
        with psycopg.connect(
            self._database_url, row_factory=dict_row
        ) as lookup_connection:
            if _select_target(lookup_connection, context, target) is None:
                self._audit_denied(context, target)
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = _lock_target(connection, context, target)
            if row is None:
                message = "A scoped cancellation target disappeared."
                raise RuntimeError(message)
            existing = _select_cancellation(connection, target, row["row_id"])
            if existing is not None and existing["final_state"] is not None:
                prior_state = ExecutionState(existing["prior_state"])
                final_state = ExecutionState(existing["final_state"])
                _insert_audit(
                    connection,
                    context,
                    target,
                    row,
                    permission_result="allowed",
                    final_result=final_state.value,
                    evidence=existing["adapter_stop_evidence"],
                    prior_state=prior_state,
                )
                return CancellationResult(
                    status=_status(connection, target, row),
                    too_late=False,
                    reconcile_deadline=existing["reconcile_deadline"],
                    evidence=_evidence_values(existing["adapter_stop_evidence"]),
                )
            if ExecutionState(row["state"]) in TERMINAL_STATES:
                terminal_state = ExecutionState(row["state"])
                _insert_audit(
                    connection,
                    context,
                    target,
                    row,
                    permission_result="allowed",
                    final_result="too_late",
                    prior_state=terminal_state,
                )
                return CancellationResult(
                    status=_status(connection, target, row),
                    too_late=True,
                    reconcile_deadline=None,
                )
            if existing is not None:
                prior_state = ExecutionState(existing["prior_state"])
                _insert_audit(
                    connection,
                    context,
                    target,
                    row,
                    permission_result="allowed",
                    final_result="pending",
                    evidence=existing["adapter_stop_evidence"],
                    prior_state=prior_state,
                )
            else:
                prior_state = ExecutionState(row["state"])
                _insert_cancellation(
                    connection,
                    context,
                    target,
                    row,
                    prior_state,
                    reason=reason,
                )
                row = _transition_locked(
                    connection,
                    target,
                    row,
                    ExecutionState.CANCEL_REQUESTED,
                    safe_error=None,
                    owner_epoch=_current_owner_epoch(connection, target, row),
                )
                _insert_audit(
                    connection,
                    context,
                    target,
                    row,
                    permission_result="allowed",
                    final_result="accepted",
                    prior_state=prior_state,
                )
        evidence = tuple(
            _call_stop(stop, ordinal) for ordinal, stop in enumerate(active_stops, 1)
        )
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_model_attempts_before_target(connection, context, target)
            row = _lock_target(connection, context, target)
            if row is None:
                message = "A cancellation target disappeared before stop evidence."
                raise RuntimeError(message)
            durable_cancellation = _select_cancellation(
                connection, target, row["row_id"]
            )
            if (
                durable_cancellation is not None
                and durable_cancellation["final_state"] is not None
            ):
                durable_evidence = _evidence_values(
                    durable_cancellation["adapter_stop_evidence"]
                )
                _insert_audit(
                    connection,
                    context,
                    target,
                    row,
                    permission_result="allowed",
                    final_result=str(durable_cancellation["final_state"]),
                    evidence=durable_cancellation["adapter_stop_evidence"],
                    prior_state=ExecutionState(durable_cancellation["prior_state"]),
                )
                return CancellationResult(
                    status=_status(connection, target, row),
                    too_late=False,
                    reconcile_deadline=durable_cancellation["reconcile_deadline"],
                    evidence=durable_evidence,
                )
            if durable_cancellation is None:
                message = "Cancellation intent disappeared before evidence update."
                raise RuntimeError(message)
            prior_evidence = _evidence_values(
                durable_cancellation["adapter_stop_evidence"]
            )
            active_operation_ids = _active_operation_ids(
                connection, target, row["row_id"]
            )
            cumulative_evidence = (*prior_evidence, *evidence)
            confirmed_operation_ids = {
                item.operation_id
                for item in cumulative_evidence
                if item.confirmed_stopped
            }
            proved_stopped = active_operation_ids <= confirmed_operation_ids
            document = [item.document() for item in evidence]
            _update_cancellation_evidence(
                connection,
                target,
                row["row_id"],
                document,
                final_state=ExecutionState.CANCELLED if proved_stopped else None,
            )
            if proved_stopped:
                _close_active_work(
                    connection, target, row["row_id"], active_operation_ids
                )
                row = _transition_locked(
                    connection,
                    target,
                    row,
                    ExecutionState.CANCELLED,
                    safe_error=None,
                    owner_epoch=_current_owner_epoch(connection, target, row),
                )
            _insert_audit(
                connection,
                context,
                target,
                row,
                permission_result="allowed",
                final_result="cancelled" if proved_stopped else "pending",
                evidence=[item.document() for item in cumulative_evidence],
                prior_state=prior_state,
            )
            return CancellationResult(
                status=_status(connection, target, row),
                too_late=False,
                reconcile_deadline=durable_cancellation["reconcile_deadline"],
                evidence=cumulative_evidence,
            )

    def reconcile_cancellation(
        self, context: RequestContext, target: ExecutionTarget
    ) -> CancellationResult:
        """Use database time and finish unproved cancellation at ten minutes."""
        _require_internal_authority(context, target)
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            _lock_model_attempts_before_target(connection, context, target)
            row = _lock_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            cancellation = _select_cancellation(connection, target, row["row_id"])
            if cancellation is None:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_TRANSITION, context.request_id
                )
            evidence = _evidence_values(cancellation["adapter_stop_evidence"])
            if ExecutionState(row["state"]) in TERMINAL_STATES:
                return CancellationResult(
                    status=_status(connection, target, row),
                    too_late=False,
                    reconcile_deadline=cancellation["reconcile_deadline"],
                    evidence=evidence,
                )
            now = connection.execute(
                "SELECT transaction_timestamp() AS current_time"
            ).fetchone()
            if now is None:
                message = "A database time query did not return a row."
                raise RuntimeError(message)
            if now["current_time"] < cancellation["reconcile_deadline"]:
                return CancellationResult(
                    status=_status(connection, target, row),
                    too_late=False,
                    reconcile_deadline=cancellation["reconcile_deadline"],
                    evidence=evidence,
                )
            _update_cancellation_evidence(
                connection,
                target,
                row["row_id"],
                [],
                final_state=ExecutionState.UNCERTAIN,
            )
            owner_epoch = _current_owner_epoch(connection, target, row)
            _mark_active_work_uncertain(connection, target, row["row_id"])
            row = _transition_locked(
                connection,
                target,
                row,
                ExecutionState.UNCERTAIN,
                safe_error=TerminalError(
                    TerminalErrorClass.UNCERTAIN_EFFECT,
                    ErrorScope.LOGICAL_REQUEST,
                    "Router could not prove that all active work stopped.",
                ),
                owner_epoch=owner_epoch,
            )
            if target.kind is ExecutionKind.AGENT_RUN:
                _fence_run_lease(connection, row["row_id"])
            _insert_audit(
                connection,
                context,
                target,
                row,
                permission_result="allowed",
                final_result="uncertain",
                evidence=[item.document() for item in evidence],
            )
            return CancellationResult(
                status=_status(connection, target, row),
                too_late=False,
                reconcile_deadline=cancellation["reconcile_deadline"],
                evidence=evidence,
            )

    def take_over_run(  # noqa: PLR0913 -- Lease fencing inputs are explicit.
        self,
        context: RequestContext,
        run_id: str,
        *,
        owner_node_id: str,
        control_epoch: int,
        expected_lease_generation: int,
        lease_duration: timedelta,
    ) -> RunLease:
        """Take an expired run lease with increasing owner and lease fences."""
        target = ExecutionTarget(ExecutionKind.AGENT_RUN, run_id)
        if not (
            context.actor_kind is PrincipalKind.SYSTEM
            and context.authority_class is AuthorityClass.SYSTEM
            and context.scope.service_id is not None
            and lease_duration > timedelta(0)
        ):
            raise ExecutionError(
                ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id
            )
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            row = _lock_target(connection, context, target)
            if row is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            lease = connection.execute(
                "SELECT * FROM router.run_leases WHERE run_row_id = %s FOR UPDATE",
                (row["row_id"],),
            ).fetchone()
            now = connection.execute(
                "SELECT transaction_timestamp() AS current_time"
            ).fetchone()
            if now is None:
                message = "A database time query did not return a row."
                raise RuntimeError(message)
            if (
                lease is None
                or int(lease["lease_generation"]) != expected_lease_generation
                or lease["expires_at"] > now["current_time"]
                or ExecutionState(row["state"]) in TERMINAL_STATES
            ):
                raise ExecutionError(
                    ExecutionErrorCode.OWNER_FENCED, context.request_id
                )
            updated = connection.execute(
                """UPDATE router.run_leases
                       SET owner_node_id = %s, control_epoch = %s,
                           owner_epoch = owner_epoch + 1,
                           lease_generation = lease_generation + 1,
                           expires_at = transaction_timestamp() + %s,
                           updated_at = transaction_timestamp()
                       WHERE run_row_id = %s RETURNING *""",
                (owner_node_id, control_epoch, lease_duration, row["row_id"]),
            ).fetchone()
            if updated is None:
                message = "A run lease update did not return a row."
                raise RuntimeError(message)
            unresolved = connection.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM router.effect_intents
                       WHERE run_row_id = %s AND state = 'intent'
                         AND owner_epoch < %s
                   ) AS unresolved""",
                (row["row_id"], updated["owner_epoch"]),
            ).fetchone()
            if unresolved is None:
                message = "An unresolved-effect query did not return a row."
                raise RuntimeError(message)
            if unresolved["unresolved"]:
                connection.execute(
                    """UPDATE router.effect_intents
                       SET state = 'uncertain', resolved_at = transaction_timestamp()
                       WHERE run_row_id = %s AND state = 'intent'
                         AND owner_epoch < %s""",
                    (row["row_id"], updated["owner_epoch"]),
                )
                _transition_locked(
                    connection,
                    target,
                    row,
                    ExecutionState.UNCERTAIN,
                    safe_error=TerminalError(
                        TerminalErrorClass.UNCERTAIN_EFFECT,
                        ErrorScope.LOGICAL_REQUEST,
                        "A prior run owner left an unresolved business effect.",
                    ),
                    owner_epoch=int(updated["owner_epoch"]),
                )
                final_lease = connection.execute(
                    """UPDATE router.run_leases
                       SET lease_generation = lease_generation + 1,
                           expires_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                       WHERE run_row_id = %s RETURNING *""",
                    (row["row_id"],),
                ).fetchone()
                if final_lease is None:
                    message = "A terminal run lease fence did not return a row."
                    raise RuntimeError(message)
                updated = final_lease
            return RunLease(
                run_id,
                str(updated["owner_node_id"]),
                int(updated["control_epoch"]),
                int(updated["owner_epoch"]),
                int(updated["lease_generation"]),
                updated["expires_at"],
            )

    def _audit_denied(self, context: RequestContext, target: ExecutionTarget) -> None:
        if context.scope.service_id is None or context.actor_kind not in {
            PrincipalKind.SERVICE,
            PrincipalKind.SYSTEM,
        }:
            return
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            _insert_audit(
                connection,
                context,
                target,
                None,
                permission_result="denied",
                final_result="denied",
            )


def _target_columns(target: ExecutionTarget) -> tuple[str, str, str]:
    if target.kind is ExecutionKind.AGENT_RUN:
        return "run_row_id", "router.agent_runs", "run_id"
    return "request_row_id", "router.logical_requests", "request_id"


def _select_target(
    connection: Connection[Any], context: RequestContext, target: ExecutionTarget
) -> dict[str, Any] | None:
    _, table, public_column = _target_columns(target)
    kind_filter = (
        "" if target.kind is ExecutionKind.AGENT_RUN else "AND request_kind = %s"
    )
    parameters: tuple[object, ...] = (
        target.public_id,
        context.scope.service_id,
        context.scope.workspace_id,
    )
    if target.kind is not ExecutionKind.AGENT_RUN:
        parameters += (target.kind.value,)
    row = connection.execute(
        f"""SELECT * FROM {table}
            WHERE {public_column} = %s AND service_id = %s
              AND workspace_id IS NOT DISTINCT FROM %s {kind_filter}
              AND (terminal_at IS NULL OR expires_at > transaction_timestamp())""",  # noqa: S608  # nosec B608
        parameters,
    ).fetchone()
    return None if row is None else dict(row)


def _lock_target(
    connection: Connection[Any], context: RequestContext, target: ExecutionTarget
) -> dict[str, Any] | None:
    _, table, public_column = _target_columns(target)
    kind_filter = (
        "" if target.kind is ExecutionKind.AGENT_RUN else "AND request_kind = %s"
    )
    parameters: tuple[object, ...] = (
        target.public_id,
        context.scope.service_id,
        context.scope.workspace_id,
    )
    if target.kind is not ExecutionKind.AGENT_RUN:
        parameters += (target.kind.value,)
    row = connection.execute(
        f"""SELECT * FROM {table}
            WHERE {public_column} = %s AND service_id = %s
              AND workspace_id IS NOT DISTINCT FROM %s {kind_filter}
              AND (terminal_at IS NULL OR expires_at > transaction_timestamp())
            FOR UPDATE""",  # noqa: S608  # nosec B608
        parameters,
    ).fetchone()
    return None if row is None else dict(row)


def _status(
    connection: Connection[Any], target: ExecutionTarget, row: dict[str, Any]
) -> ExecutionStatus:
    owner_epoch: int | None = None
    if target.kind is ExecutionKind.AGENT_RUN:
        lease = connection.execute(
            "SELECT owner_epoch FROM router.run_leases WHERE run_row_id = %s",
            (row["row_id"],),
        ).fetchone()
        owner_epoch = None if lease is None else int(lease["owner_epoch"])
        admission = ExecutionAdmission(
            target.public_id,
            target.public_id,
            row["admitted_at"],
            ExecutionState.ADMITTED,
            1,
            row["status_location"],
            row["cancel_location"],
            row["events_location"],
            "rfc8785-sha256-v1",
            bool(row["capture_enabled"]),
            str(row["capture_reason"]),
        )
        status_url = row["status_location"]
        cancel_url = row["cancel_location"]
        events_url = row["events_location"]
    else:
        admission = _logical_admission(row)
        status_url = row["status_location"]
        cancel_url = row["cancel_location"]
        events_url = row["events_location"]
    return ExecutionStatus(
        target,
        ExecutionState(row["state"]),
        int(row["state_revision"]),
        row["admitted_at"],
        row["last_transition_at"],
        row["terminal_at"],
        TerminalError.from_document(row["safe_error"]),
        bool(row["partial_output"]),
        bool(row["committed_effect"]),
        str(row["configuration_revision_id"]),
        admission,
        status_url,
        cancel_url,
        events_url,
        owner_epoch,
    )


def _logical_admission(row: dict[str, Any]) -> ExecutionAdmission:
    return ExecutionAdmission(
        str(row["request_id"]),
        None,
        row["admitted_at"],
        ExecutionState.ADMITTED,
        1,
        row["status_location"],
        row["cancel_location"],
        row["events_location"],
        "rfc8785-sha256-v1",
        bool(row["capture_enabled"]),
        str(row["capture_reason"]),
    )


def _transition_locked(  # noqa: C901, PLR0912, PLR0913
    connection: Connection[Any],
    target: ExecutionTarget,
    row: dict[str, Any],
    new_state: ExecutionState,
    *,
    safe_error: TerminalError | None,
    owner_epoch: int | None = None,
    tool_call_id: str | None = None,
    tool_expires_at: datetime | None = None,
) -> dict[str, Any]:
    current_state = ExecutionState(row["state"])
    transitions = (
        _RUN_TRANSITIONS
        if target.kind is ExecutionKind.AGENT_RUN
        else _LOGICAL_TRANSITIONS
    )
    if new_state not in transitions.get(current_state, frozenset()):
        raise ExecutionError(ExecutionErrorCode.INVALID_TRANSITION, target.public_id)
    if safe_error is not None and new_state not in {
        ExecutionState.FAILED,
        ExecutionState.INTERRUPTED,
        ExecutionState.CANCELLED,
        ExecutionState.UNCERTAIN,
    }:
        raise ExecutionError(ExecutionErrorCode.INVALID_TRANSITION, target.public_id)
    if target.kind is ExecutionKind.AGENT_RUN:
        cancellation_transition = (
            new_state
            in {
                ExecutionState.CANCEL_REQUESTED,
                ExecutionState.CANCELLED,
                ExecutionState.UNCERTAIN,
            }
            and _select_cancellation(connection, target, row["row_id"]) is not None
        )
        live_owner = _current_owner_epoch(connection, target, row)
        owner_matches = owner_epoch is not None and owner_epoch == live_owner
        if not owner_matches and not (cancellation_transition and owner_epoch is None):
            raise ExecutionError(ExecutionErrorCode.OWNER_FENCED, target.public_id)
    _, table, _ = _target_columns(target)
    terminal = new_state in TERMINAL_STATES
    timing = connection.execute(
        """SELECT transaction_timestamp() AS transition_time,
                  date_trunc('milliseconds', transaction_timestamp()) AS event_time"""
    ).fetchone()
    if timing is None:
        message = "A transition time query did not return a row."
        raise RuntimeError(message)
    transition_time = timing["transition_time"]
    event_time = timing["event_time"]
    next_revision = int(row["state_revision"]) + 1
    event_name = {
        ExecutionState.RUNNING: "request.running",
        ExecutionState.WAITING_FOR_TOOL: "request.waiting_for_tool",
        ExecutionState.CANCEL_REQUESTED: "request.cancel_requested",
    }.get(new_state, "request.terminal")
    payload: dict[str, object] = {"state_revision": next_revision}
    if event_name == "request.waiting_for_tool":
        if not tool_call_id or tool_expires_at is None:
            msg = "A waiting state needs a tool identity and expiry."
            raise ValueError(msg)
        if tool_expires_at.tzinfo is None or tool_expires_at.utcoffset() is None:
            message = "A tool wait expiry must include a time zone."
            raise ValueError(message)
        normalized_tool_expires_at = tool_expires_at.astimezone(UTC).replace(
            microsecond=(tool_expires_at.microsecond // 1000) * 1000
        )
        if not (
            event_time
            < normalized_tool_expires_at
            <= event_time + timedelta(seconds=MAXIMUM_TOOL_WAIT_SECONDS)
        ):
            message = "A tool wait expiry must be within 15 minutes."
            raise ValueError(message)
        payload.update(
            {
                "tool_call_id": tool_call_id,
                "expires_at": normalized_tool_expires_at.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
            }
        )
    if event_name == "request.terminal":
        payload.update(
            {
                "state": new_state.value,
                "partial_output": bool(row["partial_output"]),
                "committed_effects": bool(row["committed_effect"]),
            }
        )
        if safe_error is not None:
            payload["error"] = safe_error.document()
    _append_locked(
        connection,
        target,
        row,
        event_name,
        payload,
        owner_epoch=owner_epoch,
        occurred_at=event_time,
        expires_at=(
            transition_time + timedelta(seconds=STREAM_REPLAY_AFTER_TERMINAL_SECONDS)
            if terminal
            else None
        ),
    )
    if terminal:
        target_column, _, _ = _target_columns(target)
        connection.execute(
            f"""UPDATE router.execution_stream_events
                SET expires_at = %s
                WHERE {target_column} = %s AND expires_at IS NULL""",  # noqa: S608  # nosec B608
            (
                transition_time
                + timedelta(seconds=STREAM_REPLAY_AFTER_TERMINAL_SECONDS),
                row["row_id"],
            ),
        )
    updated = connection.execute(
        f"""UPDATE {table}
            SET state = %s, state_revision = %s,
                last_transition_at = %s,
                terminal_at = CASE WHEN %s THEN %s ELSE NULL END,
                expires_at = CASE WHEN %s THEN %s + interval '24 hours'
                                  ELSE expires_at END,
                safe_error = %s
            WHERE row_id = %s RETURNING *""",  # noqa: S608  # nosec B608
        (
            new_state.value,
            next_revision,
            transition_time,
            terminal,
            transition_time,
            terminal,
            transition_time,
            None if safe_error is None else Jsonb(safe_error.document()),
            row["row_id"],
        ),
    ).fetchone()
    if updated is None:
        raise ExecutionError(ExecutionErrorCode.REVISION_CONFLICT, target.public_id)
    return dict(updated)


def _set_commit_indicators(
    connection: Connection[Any],
    target: ExecutionTarget,
    row_id: object,
    *,
    partial_output: bool = False,
    committed_effect: bool = False,
) -> dict[str, Any]:
    _, table, _ = _target_columns(target)
    row = connection.execute(
        f"""UPDATE {table}
            SET partial_output = partial_output OR %s,
                committed_effect = committed_effect OR %s
            WHERE row_id = %s
            RETURNING *""",  # noqa: S608  # nosec B608
        (partial_output, committed_effect, row_id),
    ).fetchone()
    if row is None:
        message = "A commit-indicator update did not return a row."
        raise RuntimeError(message)
    return dict(row)


def _append_locked(  # noqa: PLR0913 -- Durable event fields are explicit.
    connection: Connection[Any],
    target: ExecutionTarget,
    row: dict[str, Any],
    event_name: str,
    payload: dict[str, object],
    *,
    expected_sequence: int | None = None,
    owner_epoch: int | None = None,
    occurred_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> StreamEvent:
    target_column, _, _ = _target_columns(target)
    latest = connection.execute(
        f"""SELECT sequence FROM router.execution_stream_events
            WHERE {target_column} = %s ORDER BY sequence DESC LIMIT 1""",  # noqa: S608  # nosec B608
        (row["row_id"],),
    ).fetchone()
    sequence = 1 if latest is None else int(latest["sequence"]) + 1
    if target.kind is ExecutionKind.AGENT_RUN and sequence > 1 and owner_epoch is None:
        cancellation_event = (
            event_name
            in {
                "request.cancel_requested",
                "request.terminal",
            }
            and _select_cancellation(connection, target, row["row_id"]) is not None
        )
        if not cancellation_event:
            raise ExecutionError(ExecutionErrorCode.OWNER_FENCED, target.public_id)
    if expected_sequence is not None and expected_sequence < sequence:
        existing = connection.execute(
            f"""SELECT * FROM router.execution_stream_events
                WHERE {target_column} = %s AND sequence = %s""",  # noqa: S608  # nosec B608
            (row["row_id"], expected_sequence),
        ).fetchone()
        if existing is not None:
            occurred_at = existing["occurred_at"]
            candidate = make_event(
                target,
                sequence=expected_sequence,
                event_name=event_name,
                occurred_at=occurred_at,
                payload=payload,
                expires_at=existing["expires_at"],
            )
            if (
                candidate.wire_data == existing["wire_data"]
                and event_name == existing["event_name"]
            ):
                return _stored_event(target, existing)
        raise ExecutionError(ExecutionErrorCode.STREAM_CONFLICT, target.public_id)
    if expected_sequence is not None and expected_sequence != sequence:
        raise ExecutionError(ExecutionErrorCode.STREAM_CONFLICT, target.public_id)
    if occurred_at is None:
        now = connection.execute(
            "SELECT date_trunc('milliseconds', transaction_timestamp()) AS event_time"
        ).fetchone()
        if now is None:
            message = "An event time query did not return a row."
            raise RuntimeError(message)
        occurred_at = now["event_time"]
    event = make_event(
        target,
        sequence=sequence,
        event_name=event_name,
        occurred_at=occurred_at,
        payload=payload,
        expires_at=expires_at,
    )
    connection.execute(
        f"""INSERT INTO router.execution_stream_events (
                {target_column}, service_id, workspace_id, sequence, event_name,
                occurred_at, wire_data, wire_sha256, owner_epoch, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",  # noqa: S608  # nosec B608
        (
            row["row_id"],
            row["service_id"],
            row["workspace_id"],
            sequence,
            event_name,
            event.occurred_at,
            event.wire_data,
            hashlib.sha256(event.wire_data.encode()).digest(),
            owner_epoch,
            expires_at,
        ),
    )
    return event


def _stored_event(target: ExecutionTarget, row: dict[str, Any]) -> StreamEvent:
    if hashlib.sha256(row["wire_data"].encode()).digest() != bytes(row["wire_sha256"]):
        raise ExecutionError(ExecutionErrorCode.STREAM_CONFLICT, target.public_id)
    try:
        envelope = json.loads(row["wire_data"])
    except (TypeError, ValueError) as error:
        raise ExecutionError(
            ExecutionErrorCode.STREAM_CONFLICT, target.public_id
        ) from error
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise ExecutionError(ExecutionErrorCode.STREAM_CONFLICT, target.public_id)
    try:
        validated = make_event(
            target,
            sequence=int(row["sequence"]),
            event_name=row["event_name"],
            occurred_at=row["occurred_at"],
            payload=envelope["payload"],
            expires_at=row["expires_at"],
        )
        canonical_wire = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (ExecutionError, KeyError, TypeError, ValueError) as error:
        raise ExecutionError(
            ExecutionErrorCode.STREAM_CONFLICT, target.public_id
        ) from error
    if validated.wire_data != canonical_wire:
        raise ExecutionError(ExecutionErrorCode.STREAM_CONFLICT, target.public_id)
    return StreamEvent(
        validated.target,
        validated.sequence,
        validated.event_name,
        validated.occurred_at,
        validated.payload,
        row["wire_data"],
        validated.expires_at,
    )


def _insert_cancellation(  # noqa: PLR0913 -- Durable audit inputs are explicit.
    connection: Connection[Any],
    context: RequestContext,
    target: ExecutionTarget,
    row: dict[str, Any],
    prior_state: ExecutionState,
    *,
    reason: str,
) -> dict[str, Any]:
    target_column, _, _ = _target_columns(target)
    result = connection.execute(
        f"""INSERT INTO router.execution_cancellations (
                {target_column}, service_id, workspace_id, actor_kind, actor_id,
                prior_state, reason_sha256, requested_at, reconcile_deadline
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, transaction_timestamp(),
                      transaction_timestamp() + interval '{CANCELLATION_RECONCILIATION_SECONDS} seconds')
            RETURNING *""",  # noqa: E501, S608  # nosec B608
        (
            row["row_id"],
            row["service_id"],
            row["workspace_id"],
            context.actor_kind.value,
            context.actor_id,
            prior_state.value,
            hashlib.sha256(reason.encode()).digest(),
        ),
    ).fetchone()
    if result is None:
        message = "A cancellation insert did not return a row."
        raise RuntimeError(message)
    return dict(result)


def _select_cancellation(
    connection: Connection[Any], target: ExecutionTarget, row_id: object
) -> dict[str, Any] | None:
    target_column, _, _ = _target_columns(target)
    row = connection.execute(
        f"SELECT * FROM router.execution_cancellations "  # noqa: S608  # nosec B608
        f"WHERE {target_column} = %s FOR UPDATE",
        (row_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _update_cancellation_evidence(
    connection: Connection[Any],
    target: ExecutionTarget,
    row_id: object,
    evidence: list[dict[str, object]],
    *,
    final_state: ExecutionState | None,
) -> None:
    target_column, _, _ = _target_columns(target)
    current = connection.execute(
        f"SELECT adapter_stop_evidence FROM router.execution_cancellations "  # noqa: S608  # nosec B608
        f"WHERE {target_column} = %s FOR UPDATE",
        (row_id,),
    ).fetchone()
    if current is None:
        message = "Cancellation evidence disappeared during update."
        raise RuntimeError(message)
    combined = list(current["adapter_stop_evidence"])
    combined.extend(evidence)
    connection.execute(
        f"""UPDATE router.execution_cancellations
            SET adapter_stop_evidence = %s, evidence_updated_at = transaction_timestamp(),
                final_state = %s,
                completed_at = CASE WHEN %s::router.execution_state IS NULL
                                    THEN NULL ELSE transaction_timestamp() END
            WHERE {target_column} = %s""",  # noqa: E501, S608  # nosec B608
        (
            Jsonb(combined),
            None if final_state is None else final_state.value,
            None if final_state is None else final_state.value,
            row_id,
        ),
    )


def _active_operation_ids(
    connection: Connection[Any], target: ExecutionTarget, row_id: object
) -> set[str]:
    if target.kind is ExecutionKind.AGENT_RUN:
        rows = connection.execute(
            """SELECT operation_identity AS operation_identity
               FROM router.effect_intents
               WHERE run_row_id = %s AND state = 'intent'
               FOR UPDATE""",
            (row_id,),
        ).fetchall()
        lease = connection.execute(
            """SELECT lease.owner_epoch FROM router.run_leases AS lease
               JOIN router.agent_runs AS run ON run.row_id = lease.run_row_id
               WHERE lease.run_row_id = %s
                 AND run.state IN ('running','waiting_for_tool','cancel_requested')
               FOR UPDATE OF lease""",
            (row_id,),
        ).fetchone()
        result = {str(row["operation_identity"]) for row in rows}
        if lease is not None:
            result.add(f"run-owner:{lease['owner_epoch']}")
        return result
    rows = connection.execute(
        """SELECT id::text AS operation_identity
           FROM router.provider_attempts
           WHERE request_row_id = %s AND state = 'started'
           FOR UPDATE""",
        (row_id,),
    ).fetchall()
    return {str(row["operation_identity"]) for row in rows}


def _lock_model_attempts_before_target(
    connection: Connection[Any], context: RequestContext, target: ExecutionTarget
) -> None:
    """Use the routing attempt-to-request lock order before cancellation writes."""
    if target.kind is not ExecutionKind.MODEL:
        return
    connection.execute(
        """SELECT attempt.id
           FROM router.provider_attempts AS attempt
           JOIN router.logical_requests AS request
             ON request.row_id = attempt.request_row_id
           WHERE request.request_id = %s AND request.service_id = %s
             AND request.workspace_id IS NOT DISTINCT FROM %s
             AND attempt.state = 'started'
           ORDER BY attempt.id
           FOR UPDATE OF attempt""",
        (target.public_id, context.scope.service_id, context.scope.workspace_id),
    ).fetchall()


def _close_active_work(
    connection: Connection[Any],
    target: ExecutionTarget,
    row_id: object,
    evidence_ids: set[str],
) -> None:
    if not evidence_ids:
        return
    if target.kind is ExecutionKind.AGENT_RUN:
        owner_evidence = [
            value for value in evidence_ids if value.startswith("run-owner:")
        ]
        connection.execute(
            """UPDATE router.effect_intents
               SET state = 'failed', resolved_at = transaction_timestamp()
               WHERE run_row_id = %s AND state = 'intent'
                 AND operation_identity = ANY(%s)""",
            (row_id, list(evidence_ids)),
        )
        if owner_evidence:
            connection.execute(
                """UPDATE router.run_leases
                   SET lease_generation = lease_generation + 1,
                       expires_at = transaction_timestamp(),
                       updated_at = transaction_timestamp()
                   WHERE run_row_id = %s AND owner_epoch = %s""",
                (row_id, int(owner_evidence[0].split(":", 1)[1])),
            )
        return
    _finish_routing_attempts(
        connection,
        row_id,
        state="cancelled",
        error_class="cancelled",
        fallback_decision="cancelled",
        detail_code="cancel_confirmed",
        attempt_ids=evidence_ids,
    )


def _mark_active_work_uncertain(
    connection: Connection[Any], target: ExecutionTarget, row_id: object
) -> None:
    if target.kind is ExecutionKind.AGENT_RUN:
        connection.execute(
            """UPDATE router.effect_intents
               SET state = 'uncertain', resolved_at = transaction_timestamp()
               WHERE run_row_id = %s AND state = 'intent'""",
            (row_id,),
        )
        return
    _finish_routing_attempts(
        connection,
        row_id,
        state="uncertain",
        error_class="uncertain_effect",
        fallback_decision="commit_boundary",
        detail_code="cancellation_unconfirmed",
    )


def _finish_routing_attempts(  # noqa: PLR0913
    connection: Connection[Any],
    row_id: object,
    *,
    state: str,
    error_class: str,
    fallback_decision: str,
    detail_code: str,
    attempt_ids: set[str] | None = None,
) -> None:
    rows = connection.execute(
        """UPDATE router.provider_attempts AS attempt
           SET state = %s, finished_at = transaction_timestamp(),
               normalized_error_class = %s, affected_scope = 'logical_request',
               affected_scope_id = request.request_id::text,
               retry_decision = %s, safe_provider_code = NULL,
               redacted_evidence = jsonb_build_object(
                   'provider_status', NULL, 'retry_after_ms', NULL,
                   'detail_code', %s::text
               )
           FROM router.logical_requests AS request
           WHERE attempt.request_row_id = %s AND attempt.state = 'started'
             AND request.row_id = attempt.request_row_id
             AND (%s::uuid[] IS NULL OR attempt.id = ANY(%s::uuid[]))
           RETURNING attempt.*""",
        (
            state,
            error_class,
            fallback_decision,
            detail_code,
            row_id,
            None if attempt_ids is None else list(attempt_ids),
            None if attempt_ids is None else list(attempt_ids),
        ),
    ).fetchall()
    for attempt in rows:
        routing = connection.execute(
            """SELECT attempt_start.claim_id, claim.claim_generation
               FROM router.routing_attempt_starts AS attempt_start
               JOIN router.routing_attempt_claims AS claim
                 ON claim.claim_id = attempt_start.claim_id
                AND claim.attempt_id = attempt_start.attempt_id
               WHERE attempt_start.attempt_id = %s""",
            (attempt["id"],),
        ).fetchone()
        if routing is None and not attempt["migration_0015_backfilled"]:
            message = "The active routing claim is unavailable."
            raise RuntimeError(message)
        sequence_row = connection.execute(
            """SELECT COALESCE(max(decision_sequence), 0) + 1 AS value
               FROM router.routing_candidate_decisions WHERE request_row_id = %s""",
            (attempt["request_row_id"],),
        ).fetchone()
        if sequence_row is None:
            message = "The routing decision sequence is unavailable."
            raise RuntimeError(message)
        connection.execute(
            """INSERT INTO router.routing_candidate_decisions (
                   decision_id, request_row_id, decision_sequence, attempt_id, claim_id,
                   claim_generation, attempt_number, candidate_ordinal,
                   route_snapshot_id,
                   connect_timeout_ms, first_byte_timeout_ms, idle_timeout_ms,
                   execution_timeout_ms, logical_deadline, attempt_deadline,
                   attempt_state, normalized_error_class, affected_scope,
                   affected_scope_id, fallback_decision, safe_provider_code,
                   redacted_evidence, occurred_at, migration_0015_backfilled
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                         'logical_request',%s,%s,NULL,
                         jsonb_build_object(
                           'provider_status', NULL, 'retry_after_ms', NULL,
                           'detail_code', %s::text
                         ), transaction_timestamp(), %s)""",
            (
                uuid.uuid4(),
                attempt["request_row_id"],
                sequence_row["value"],
                attempt["id"],
                None if routing is None else routing["claim_id"],
                None if routing is None else routing["claim_generation"],
                attempt["attempt_number"],
                attempt["candidate_ordinal"],
                attempt["route_snapshot_id"],
                attempt["connect_timeout_ms"],
                attempt["first_byte_timeout_ms"],
                attempt["idle_timeout_ms"],
                attempt["execution_timeout_ms"],
                attempt["logical_deadline"],
                attempt["attempt_deadline"],
                state,
                error_class,
                attempt["affected_scope_id"],
                fallback_decision,
                detail_code,
                attempt["migration_0015_backfilled"],
            ),
        )
        connection.execute(
            """DELETE FROM router.routing_attempt_claims
               WHERE request_row_id = %s AND attempt_id = %s""",
            (attempt["request_row_id"], attempt["id"]),
        )


def _fence_run_lease(connection: Connection[Any], row_id: object) -> None:
    connection.execute(
        """UPDATE router.run_leases
           SET lease_generation = lease_generation + 1,
               expires_at = transaction_timestamp(),
               updated_at = transaction_timestamp()
           WHERE run_row_id = %s""",
        (row_id,),
    )


def _current_owner_epoch(
    connection: Connection[Any], target: ExecutionTarget, row: dict[str, Any]
) -> int | None:
    if target.kind is not ExecutionKind.AGENT_RUN:
        return None
    lease = connection.execute(
        """SELECT owner_epoch FROM router.run_leases
           WHERE run_row_id = %s AND expires_at > transaction_timestamp()""",
        (row["row_id"],),
    ).fetchone()
    return None if lease is None else int(lease["owner_epoch"])


def _insert_audit(  # noqa: PLR0913 -- Durable audit fields are explicit.
    connection: Connection[Any],
    context: RequestContext,
    target: ExecutionTarget,
    row: dict[str, Any] | None,
    *,
    permission_result: str,
    final_result: str,
    evidence: Sequence[object] = (),
    prior_state: ExecutionState | None = None,
) -> None:
    request_row_id = None
    run_row_id = None
    if row is not None:
        if target.kind is ExecutionKind.AGENT_RUN:
            run_row_id = row["row_id"]
        else:
            request_row_id = row["row_id"]
    connection.execute(
        """INSERT INTO router.execution_cancellation_audit (
               event_id, request_row_id, run_row_id, target_public_id,
               service_id, workspace_id, actor_kind, actor_id, permission_result,
               action, prior_state, adapter_stop_evidence, final_result, occurred_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     transaction_timestamp())""",
        (
            uuid.uuid4(),
            request_row_id,
            run_row_id,
            uuid.UUID(target.public_id),
            context.scope.service_id,
            context.scope.workspace_id,
            context.actor_kind.value,
            context.actor_id,
            permission_result,
            {
                ExecutionKind.MODEL: "model.cancel",
                ExecutionKind.SHARED_TOOL: "tool.cancel",
                ExecutionKind.AGENT_RUN: "run.cancel",
            }[target.kind],
            None if prior_state is None else prior_state.value,
            Jsonb(list(evidence)),
            final_result,
        ),
    )


def _evidence_values(value: object) -> tuple[AdapterStopEvidence, ...]:
    if not isinstance(value, list):
        message = "Stored adapter stop evidence must be an array."
        raise TypeError(message)
    result: list[AdapterStopEvidence] = []
    required = {
        "operation_id",
        "supported",
        "stop_requested",
        "confirmed_stopped",
        "safe_code",
    }
    for item in value:
        if not isinstance(item, dict) or item.keys() != required:
            message = "Stored adapter stop evidence is invalid."
            raise ValueError(message)
        operation_id = item["operation_id"]
        safe_code = item["safe_code"]
        flags = (item["supported"], item["stop_requested"], item["confirmed_stopped"])
        if (
            not isinstance(operation_id, str)
            or not all(isinstance(flag, bool) for flag in flags)
            or (safe_code is not None and not isinstance(safe_code, str))
        ):
            message = "Stored adapter stop evidence is invalid."
            raise ValueError(message)
        result.append(AdapterStopEvidence(operation_id, *flags, safe_code))
    return tuple(result)


def _call_stop(stop: AdapterStop, ordinal: int) -> AdapterStopEvidence:
    try:
        result = stop()
    except Exception:  # noqa: BLE001
        result = None
    if isinstance(result, AdapterStopEvidence):
        return result
    return AdapterStopEvidence(
        operation_id=f"adapter-stop-{ordinal}",
        supported=False,
        stop_requested=True,
        confirmed_stopped=False,
        safe_code="stop_failed",
    )


def _require_read_authority(context: RequestContext, target: ExecutionTarget) -> None:
    operation = {
        ExecutionKind.MODEL: "model.read",
        ExecutionKind.SHARED_TOOL: "tool.read",
        ExecutionKind.AGENT_RUN: "run.read",
    }[target.kind]
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == operation
        and not context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise ExecutionError(ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_cancel_authority(context: RequestContext, target: ExecutionTarget) -> None:
    operation = {
        ExecutionKind.MODEL: "model.cancel",
        ExecutionKind.SHARED_TOOL: "tool.cancel",
        ExecutionKind.AGENT_RUN: "run.cancel",
    }[target.kind]
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == operation
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise ExecutionError(ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_internal_authority(
    context: RequestContext, target: ExecutionTarget
) -> None:
    if context.actor_kind is PrincipalKind.SYSTEM and (
        context.authority_class is AuthorityClass.SYSTEM
        and context.scope.service_id is not None
    ):
        return
    operation = {
        ExecutionKind.MODEL: "model.create",
        ExecutionKind.SHARED_TOOL: "tool.create",
        ExecutionKind.AGENT_RUN: "run.create",
    }[target.kind]
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == operation
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise ExecutionError(ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id)

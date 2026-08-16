"""Bounded PostgreSQL reads for native model-request status."""
# ruff: noqa: EM101, PLR2004, TRY003, TRY004

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
    ServicePrincipal,
)
from llmrouter_backend.execution import (
    ExecutionError,
    ExecutionErrorCode,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    TerminalError,
)

from .model import MAXIMUM_STATUS_ATTEMPTS, MAXIMUM_STATUS_BYTES, ResumePoint

if TYPE_CHECKING:
    from datetime import datetime


class PostgresModelRequestViews:
    """Read safe request, attempt, output, and accounting views."""

    def __init__(self, database_url: str) -> None:
        """Set one non-empty PostgreSQL address."""
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        self._database_url = database_url

    def resolve_scope(
        self, principal: ServicePrincipal, request_id: str
    ) -> Scope | None:
        """Resolve one unambiguous request only inside token scope."""
        if principal.audience is not Audience.DATA_PLANE or principal.service_id == "":
            return None
        parameters: list[object] = [request_id, principal.service_id]
        if principal.allowed_workspace_ids is not None:
            allowed = sorted(principal.allowed_workspace_ids)
            parameters.append(allowed)
            query = """SELECT service_id::text, workspace_id::text
                       FROM router.logical_requests
                       WHERE request_id = %s AND service_id = %s
                         AND request_kind = 'model'
                         AND (terminal_at IS NULL
                              OR expires_at > transaction_timestamp())
                         AND (workspace_id IS NULL
                              OR workspace_id = ANY(%s::uuid[]))
                       LIMIT 2"""
        else:
            query = """SELECT service_id::text, workspace_id::text
                       FROM router.logical_requests
                       WHERE request_id = %s AND service_id = %s
                         AND request_kind = 'model'
                         AND (terminal_at IS NULL
                              OR expires_at > transaction_timestamp())
                       LIMIT 2"""
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                query,
                tuple(parameters),
            ).fetchall()
        if len(rows) != 1:
            return None
        return Scope(rows[0]["service_id"], rows[0]["workspace_id"])

    def status(
        self, context: RequestContext, target: ExecutionTarget
    ) -> dict[str, object]:
        """Return one closed bounded public status in the exact scope."""
        _require_view_authority(context, target)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            request = connection.execute(
                """SELECT request.*, assignment.stable_name AS assignment_name
                   FROM router.logical_requests AS request
                   LEFT JOIN router.assignment_definitions AS assignment
                     ON assignment.id = request.assignment_id
                   WHERE request.request_id = %s
                     AND request.service_id = %s
                     AND request.workspace_id IS NOT DISTINCT FROM %s
                     AND request.request_kind = 'model'
                     AND (request.terminal_at IS NULL
                          OR request.expires_at > transaction_timestamp())""",
                (
                    target.public_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                ),
            ).fetchone()
            if request is None:
                raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
            attempts = _attempts(connection, request["row_id"])
            accounting = _accounting(connection, request["row_id"])
            result = _retained_result(connection, request["row_id"])
        document: dict[str, object] = {
            "request_id": str(request["request_id"]),
            "state": str(request["state"]),
            "state_revision": int(request["state_revision"]),
            "admitted_at": _timestamp(request["admitted_at"]),
            "last_transition_at": _timestamp(request["last_transition_at"]),
            "partial_output": bool(request["partial_output"]),
            "committed_effects": bool(request["committed_effect"]),
            "configuration_revision": str(request["configuration_revision_id"]),
            "admission": _admission(request),
            "attempts": attempts,
            "tool_calls": [],
            "accounting": accounting,
        }
        if request["assignment_name"] is not None:
            document["assignment"] = str(request["assignment_name"])
        else:
            document["exact_route"] = str(request["exact_route_id"])
        if request["terminal_at"] is not None:
            document["terminal_at"] = _timestamp(request["terminal_at"])
        error = TerminalError.from_document(request["safe_error"])
        if error is not None:
            document["error"] = error.document()
        if result is not None and request["state"] == "succeeded":
            document["result"] = {"outputs": [{"type": "text", "text": result}]}
        encoded_bytes = len(
            json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        if encoded_bytes > MAXIMUM_STATUS_BYTES:
            document.pop("result", None)
        return document

    def resume_point(
        self, context: RequestContext, target: ExecutionTarget
    ) -> ResumePoint:
        """Read only the exact state needed for equal-replay recovery."""
        _require_resume_authority(context, target)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT state, state_revision
                   FROM router.logical_requests
                   WHERE request_id = %s
                     AND service_id = %s
                     AND workspace_id IS NOT DISTINCT FROM %s
                     AND request_kind = 'model'
                     AND (terminal_at IS NULL
                          OR expires_at > transaction_timestamp())""",
                (
                    target.public_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                ),
            ).fetchone()
        if row is None:
            raise ExecutionError(ExecutionErrorCode.NOT_FOUND, context.request_id)
        return ResumePoint(
            ExecutionState(str(row["state"])), int(row["state_revision"])
        )


def _attempts(
    connection: psycopg.Connection[dict[str, Any]], request_row_id: object
) -> list[dict[str, object]]:
    rows = connection.execute(
        """SELECT attempt.*,
                  COALESCE(usage.usage_components, '[]'::jsonb) AS usage_components
           FROM router.provider_attempts AS attempt
           LEFT JOIN router.routing_attempt_usage_reports AS usage
             ON usage.attempt_id = attempt.id
           WHERE attempt.request_row_id = %s
           ORDER BY attempt.attempt_number
           LIMIT %s""",
        (request_row_id, MAXIMUM_STATUS_ATTEMPTS),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        state = str(row["state"])
        item: dict[str, object] = {
            "attempt_id": str(row["id"]),
            "provider_model_route_id": str(row["provider_model_route_id"]),
            "state": "running"
            if state == "started"
            else "failed"
            if state == "interrupted"
            else state,
            "started_at": _timestamp(row["started_at"]),
            "assignment_revision": str(row["assignment_revision_id"]),
            "usage": _usage(row["usage_components"]),
            "price_version": str(row["price_version_id"]),
        }
        if row["finished_at"] is not None:
            item["ended_at"] = _timestamp(row["finished_at"])
        if row["normalized_error_class"] is not None:
            error: dict[str, str] = {
                "class": str(row["normalized_error_class"]),
                "affected_scope": str(row["affected_scope"]),
                "message": "The provider attempt did not complete.",
            }
            if row["safe_provider_code"] is not None:
                error["safe_provider_code"] = str(row["safe_provider_code"])
            item["error"] = error
        result.append(item)
    return result


def _usage(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        unit = item.get("unit")
        quantity = item.get("quantity")
        if (
            not isinstance(unit, str)
            or not isinstance(quantity, str)
            or unit in seen
            or len(unit) > 100
            or len(quantity) > 100
        ):
            continue
        try:
            parsed = Decimal(quantity)
        except (ArithmeticError, ValueError):
            continue
        if not parsed.is_finite() or parsed < 0:
            continue
        seen.add(unit)
        result.append({"unit": unit, "quantity": format(parsed, "f")})
    return result


def _accounting(
    connection: psycopg.Connection[dict[str, Any]], request_row_id: object
) -> dict[str, str]:
    row = connection.execute(
        """SELECT budget.currency,
                  COALESCE(reservations.estimated, 0) AS estimated,
                  COALESCE(reservations.reserved, 0) AS reserved,
                  COALESCE(reconciliations.used, 0) AS used,
                  COALESCE(corrections.correction_delta, 0) AS correction_delta
           FROM router.logical_request_budget_sets AS budget
           LEFT JOIN LATERAL (
               SELECT sum(reservation.estimated_amount) AS estimated,
                      sum(reservation.reserved_amount) AS reserved
               FROM router.budget_candidate_reservations AS reservation
               WHERE reservation.budget_set_id = budget.id
           ) AS reservations ON true
           LEFT JOIN LATERAL (
               SELECT sum(reconciliation.actual_amount) AS used
               FROM router.budget_candidate_reservations AS reservation
               JOIN router.budget_reservation_reconciliations AS reconciliation
                 ON reconciliation.reservation_id = reservation.id
               WHERE reservation.budget_set_id = budget.id
           ) AS reconciliations ON true
           LEFT JOIN LATERAL (
               SELECT sum(correction.amount_delta) AS correction_delta
               FROM router.budget_candidate_reservations AS reservation
               JOIN router.budget_reservation_corrections AS correction
                 ON correction.reservation_id = reservation.id
               WHERE reservation.budget_set_id = budget.id
           ) AS corrections ON true
           WHERE budget.request_row_id = %s""",
        (request_row_id,),
    ).fetchone()
    if row is None:
        currency = _snapshot_currency(connection, request_row_id)
        return {
            "estimated": "0",
            "reserved": "0",
            "used": "0",
            "corrected": "0",
            "currency": currency,
        }
    used = Decimal(row["used"])
    corrected = max(Decimal(0), used + Decimal(row["correction_delta"]))
    return {
        "estimated": _decimal(row["estimated"]),
        "reserved": _decimal(row["reserved"]),
        "used": _decimal(used),
        "corrected": _decimal(corrected),
        "currency": str(row["currency"]),
    }


def _snapshot_currency(
    connection: psycopg.Connection[dict[str, Any]], request_row_id: object
) -> str:
    rows = connection.execute(
        """SELECT snapshot.typed_prices
           FROM router.provider_route_execution_snapshots AS snapshot
           WHERE snapshot.request_row_id = %s
           ORDER BY snapshot.candidate_ordinal LIMIT 1""",
        (request_row_id,),
    ).fetchone()
    if rows is not None and isinstance(rows["typed_prices"], list):
        for item in rows["typed_prices"]:
            if isinstance(item, Mapping):
                currency = item.get("currency")
                if (
                    isinstance(currency, str)
                    and len(currency) == 3
                    and currency.isascii()
                    and currency.isupper()
                ):
                    return currency
    return "USD"


def _retained_result(
    connection: psycopg.Connection[dict[str, Any]], request_row_id: object
) -> str | None:
    chunks: list[str] = []
    size = 0
    completed = False
    with connection.cursor(name="model_request_retained_result") as cursor:
        cursor.execute(
            """SELECT event_name,
                      wire_data::jsonb->'payload'->>'delta' AS delta
               FROM router.execution_stream_events
               WHERE request_row_id = %s
                 AND event_name IN ('output.delta', 'output.completed')
                 AND (expires_at IS NULL OR expires_at > transaction_timestamp())
               ORDER BY sequence""",
            (request_row_id,),
        )
        for row in cursor:
            if row["event_name"] == "output.completed":
                completed = True
                continue
            delta = row["delta"]
            if completed or not isinstance(delta, str):
                return None
            size += len(delta.encode("utf-8"))
            if size > MAXIMUM_STATUS_BYTES:
                return None
            chunks.append(delta)
    return "".join(chunks) if completed else None


def _admission(row: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "request_id": str(row["request_id"]),
        "admitted_at": _timestamp(_cast_datetime(row["admitted_at"])),
        "state": "admitted",
        "state_revision": 1,
        "status_url": str(row["status_location"]),
        "cancel_url": str(row["cancel_location"]),
        "fingerprint_version": "rfc8785-sha256-v1",
        "capture_enabled": bool(row["capture_enabled"]),
        "capture_reason": str(row["capture_reason"]),
    }
    if row["events_location"] is not None:
        result["events_url"] = str(row["events_location"])
    return result


def _require_view_authority(context: RequestContext, target: ExecutionTarget) -> None:
    if not (
        target.kind is ExecutionKind.MODEL
        and context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation in {"model.read", "model.cancel"}
        and context.mutation == (context.operation == "model.cancel")
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise ExecutionError(ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_resume_authority(context: RequestContext, target: ExecutionTarget) -> None:
    if not (
        target.kind is ExecutionKind.MODEL
        and context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
        and context.operation == "model.create"
        and context.mutation
        and context.scope.service_id is not None
        and context.actor_id == context.scope.service_id
    ):
        raise ExecutionError(ExecutionErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _decimal(value: object) -> str:
    if not isinstance(value, (Decimal, int, float, str, tuple)):
        raise RuntimeError("Stored accounting is invalid.")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError("Stored accounting is invalid.")
    return format(parsed, "f")


def _cast_datetime(value: object) -> datetime:
    if not hasattr(value, "tzinfo"):
        raise RuntimeError("A stored request time is invalid.")
    return value  # type: ignore[return-value]


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("A stored request time is invalid.")
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

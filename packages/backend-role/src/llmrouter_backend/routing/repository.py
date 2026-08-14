"""Transactional durable provider routing operations."""
# ruff: noqa: C901, D107, E501, EM101, PLR0911, PLR0912, PLR0915, PLR2004, S101, TRY003

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.accounting import PriceComponent, UsageComponent, UsageUnit
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
)
from llmrouter_backend.execution import ErrorScope, TerminalError, TerminalErrorClass

from .errors import RoutingError, RoutingErrorCode
from .model import (
    MAXIMUM_DIAGNOSTIC_GRANT_SECONDS,
    AdapterResult,
    AttemptFailure,
    AttemptOutcome,
    AttemptPlan,
    AttemptTimeouts,
    DiagnosticGrant,
    FallbackDecision,
    SafeFailureEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class PostgresRoutingRepository:
    """Serialize claims and keep every route outcome durable."""

    def __init__(
        self,
        database_url: str,
        *,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        grant_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        self._database_url = database_url
        self._identity_factory = identity_factory
        self._grant_factory = grant_factory

    def create_diagnostic_grant(
        self,
        context: RequestContext,
        *,
        exact_route_id: str,
        reason: str,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> DiagnosticGrant:
        """Create one exact short-lived grant and return its bearer once."""
        if (
            context.operation != "diagnostic.grant.create"
            or not context.mutation
            or context.actor_kind is not PrincipalKind.SERVICE
            or context.actor_id != context.scope.service_id
            or context.authority_class is not AuthorityClass.SERVICE
            or context.authority_path is not AuthorityPath.MACHINE
            or context.machine_audience is not Audience.CONFIGURATION
        ):
            raise RoutingError(RoutingErrorCode.INSUFFICIENT_SCOPE, context.request_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("The diagnostic grant time must include a time zone.")
        if (
            not reason
            or len(reason) > 500
            or any(not " " <= character <= "~" for character in reason)
        ):
            raise ValueError("The diagnostic reason is invalid.")
        if lifetime <= timedelta(0) or lifetime > timedelta(
            seconds=MAXIMUM_DIAGNOSTIC_GRANT_SECONDS
        ):
            raise ValueError("The diagnostic grant lifetime is invalid.")
        try:
            route_identity = uuid.UUID(exact_route_id)
        except ValueError as error:
            raise RoutingError(
                RoutingErrorCode.NOT_FOUND, context.request_id
            ) from error
        raw = self._grant_factory(32)
        if not 43 <= len(raw) <= 200 or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in raw
        ):
            raise ValueError(
                "The diagnostic grant generator returned an invalid bearer."
            )
        grant_id = self._identity_factory()
        event_id = self._identity_factory()
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            time_row = connection.execute(
                "SELECT transaction_timestamp() AS value"
            ).fetchone()
            assert time_row is not None
            created_at = time_row["value"]
            expires_at = created_at + lifetime
            route = connection.execute(
                """SELECT route.current_revision, credential.id AS credential_id,
                          credential.generation AS credential_generation,
                          credential.current_revision AS credential_revision_id
                   FROM router.provider_model_routes AS route
                   JOIN router.provider_instances AS instance
                     ON instance.id = route.provider_instance_id
                   JOIN router.encrypted_credentials AS credential
                     ON credential.id = instance.credential_id
                   WHERE route.id = %s AND route.current_revision IS NOT NULL
                     AND route.state = 'active'
                     AND instance.state = 'active'
                     AND credential.state = 'active'
                     AND credential.current_revision IS NOT NULL
                     AND router.active_request_scope(%s, %s)
                     AND router.provider_route_is_eligible(route.id, %s)
                     AND router.provider_resource_is_enabled(
                         'provider_model_route', route.id, %s, %s
                     )
                     AND router.provider_resource_is_enabled(
                         'provider_instance', instance.id, %s, %s
                     )
                   FOR SHARE OF route, instance, credential""",
                (
                    route_identity,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    context.scope.service_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                ),
            ).fetchone()
            if route is None:
                raise RoutingError(RoutingErrorCode.NOT_FOUND, context.request_id)
            connection.execute(
                """INSERT INTO router.audit_events (
                       event_id, audit_class, actor_kind, actor_id, authority_class,
                       service_id, workspace_id, action, permission_result,
                       safe_details, occurred_at
                   ) VALUES (%s, 'security', %s, %s, %s, %s, %s,
                             'diagnostic.grant.create', 'permitted',
                             jsonb_build_object(
                                 'diagnostic_grant_id', %s::uuid,
                                 'exact_route_id', %s::uuid,
                                 'route_configuration_revision_id', %s::uuid,
                                 'reason', %s::text, 'expires_at', %s::timestamptz
                             ), %s)""",
                (
                    event_id,
                    context.actor_kind.value,
                    context.actor_id,
                    context.authority_class.value,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    grant_id,
                    route_identity,
                    route["current_revision"],
                    reason,
                    expires_at,
                    created_at,
                ),
            )
            connection.execute(
                """INSERT INTO router.diagnostic_route_grants (
                       grant_id, grant_sha256, service_id, workspace_id, exact_route_id,
                       route_configuration_revision_id, credential_id,
                       credential_generation, credential_revision_id,
                       created_by_kind, created_by_id,
                       reason, created_at, expires_at, creation_audit_event_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    grant_id,
                    hashlib.sha256(raw.encode("ascii")).digest(),
                    context.scope.service_id,
                    context.scope.workspace_id,
                    route_identity,
                    route["current_revision"],
                    route["credential_id"],
                    route["credential_generation"],
                    route["credential_revision_id"],
                    context.actor_kind.value,
                    context.actor_id,
                    reason,
                    created_at,
                    expires_at,
                    event_id,
                ),
            )
        return DiagnosticGrant(
            str(grant_id),
            raw,
            context.scope.service_id,
            context.scope.workspace_id,
            str(route_identity),
            str(route["current_revision"]),
            expires_at,
        )

    def pending_accounting(
        self, context: RequestContext, *, request_id: str
    ) -> tuple[AttemptPlan, AdapterResult, bool] | None:
        """Return the latest durable routing result and its accounting state."""
        _require_routing_authority(context)
        try:
            uuid.UUID(request_id)
        except ValueError as error:
            raise RoutingError(
                RoutingErrorCode.NOT_FOUND, context.request_id
            ) from error
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            request = connection.execute(
                """SELECT * FROM router.logical_requests
                   WHERE request_id = %s AND service_id = %s
                     AND workspace_id IS NOT DISTINCT FROM %s""",
                (
                    request_id,
                    context.scope.service_id,
                    context.scope.workspace_id,
                ),
            ).fetchone()
            if request is None:
                return None
            request_terminal = connection.execute(
                """SELECT * FROM router.routing_request_terminal_decisions
                   WHERE request_row_id = %s""",
                (request["row_id"],),
            ).fetchone()
            if request_terminal is not None:
                snapshot = connection.execute(
                    """SELECT * FROM router.provider_route_execution_snapshots
                       WHERE id = %s""",
                    (request_terminal["route_snapshot_id"],),
                ).fetchone()
                assert snapshot is not None
                plan = _plan(
                    request,
                    snapshot,
                    _decision_claim(request_terminal),
                    recovery_only=True,
                    recovery_failure=_decision_failure(request_terminal),
                    request_terminal=True,
                )
                return plan, _decision_result(request_terminal), True
            decision = connection.execute(
                """SELECT * FROM router.routing_candidate_decisions
                   WHERE request_row_id = %s AND NOT migration_0015_backfilled
                   ORDER BY decision_sequence DESC LIMIT 1""",
                (request["row_id"],),
            ).fetchone()
            if decision is None:
                return None
            attempt = connection.execute(
                """SELECT attempt.*, attempt_start.claim_id,
                          attempt_start.claim_generation,
                          COALESCE(usage.usage_components, '[]'::jsonb)
                            AS usage_components,
                          EXISTS (
                              SELECT 1 FROM router.routing_attempt_dispatches AS dispatch
                              WHERE dispatch.attempt_id = attempt.id
                          ) AS dispatched
                   FROM router.provider_attempts AS attempt
                   JOIN router.routing_attempt_starts AS attempt_start
                     ON attempt_start.attempt_id = attempt.id
                   LEFT JOIN router.routing_attempt_usage_reports AS usage
                     ON usage.attempt_id = attempt.id
                   WHERE attempt.id = %s AND attempt.state <> 'started'
                     AND NOT attempt.migration_0015_backfilled""",
                (decision["attempt_id"],),
            ).fetchone()
            if attempt is None:
                snapshot = connection.execute(
                    """SELECT * FROM router.provider_route_execution_snapshots
                       WHERE id = %s""",
                    (decision["route_snapshot_id"],),
                ).fetchone()
                assert snapshot is not None
                plan = _plan(
                    request,
                    snapshot,
                    _decision_claim(decision),
                    recovery_only=True,
                    recovery_failure=_decision_failure(decision),
                )
                return plan, _decision_result(decision), True
            snapshot = connection.execute(
                "SELECT * FROM router.provider_route_execution_snapshots WHERE id = %s",
                (attempt["route_snapshot_id"],),
            ).fetchone()
            assert snapshot is not None
            claim = {
                "claim_id": attempt["claim_id"],
                "claim_generation": attempt["claim_generation"],
                "attempt_id": attempt["id"],
                "attempt_number": attempt["attempt_number"],
                "candidate_ordinal": attempt["candidate_ordinal"],
                "assignment_revision_id": attempt["assignment_revision_id"],
                "route_snapshot_id": attempt["route_snapshot_id"],
                "candidate_policy": snapshot["candidate_policy"],
                "connect_timeout_ms": attempt["connect_timeout_ms"],
                "first_byte_timeout_ms": attempt["first_byte_timeout_ms"],
                "idle_timeout_ms": attempt["idle_timeout_ms"],
                "execution_timeout_ms": attempt["execution_timeout_ms"],
                "logical_deadline": attempt["logical_deadline"],
                "attempt_deadline": attempt["attempt_deadline"],
            }
            plan = _plan(
                request,
                snapshot,
                claim,
                started=True,
                dispatched=attempt["dispatched"],
            )
            accounting_complete = connection.execute(
                """SELECT EXISTS (
                           SELECT 1 FROM router.accounting_facts AS fact
                           LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
                             ON reconciliation.accounting_event_id = fact.event_id
                            AND reconciliation.reservation_id = %s
                           WHERE fact.request_row_id = %s
                             AND fact.subject_kind = 'provider_attempt'
                             AND fact.subject_id = %s
                             AND fact.outcome = CASE %s
                                 WHEN 'succeeded' THEN 'succeeded'
                                 WHEN 'failed' THEN 'failed'
                                 WHEN 'interrupted' THEN 'interrupted'
                                 WHEN 'uncertain' THEN 'uncertain'
                                 WHEN 'cancelled' THEN 'failed'
                             END
                             AND fact.occurred_at >= %s
                             AND (%s IS NULL OR reconciliation.reservation_id IS NOT NULL)
                       ) AS value""",
                (
                    attempt["budget_reservation_id"],
                    request["row_id"],
                    attempt["id"],
                    attempt["state"],
                    attempt["finished_at"],
                    attempt["budget_reservation_id"],
                ),
            ).fetchone()
            assert accounting_complete is not None
            return plan, _terminal_result(attempt), accounting_complete["value"]

    def claim(
        self, context: RequestContext, *, request_id: str, owner_id: str
    ) -> AttemptPlan:
        """Claim the next eligible admitted snapshot under one request lock."""
        service_create = (
            context.actor_kind is PrincipalKind.SERVICE
            and context.actor_id == context.scope.service_id
            and context.authority_class is AuthorityClass.SERVICE
            and context.operation in {"model.create", "tool.create"}
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is Audience.DATA_PLANE
        )
        system_recovery = (
            context.actor_kind is PrincipalKind.SYSTEM
            and context.authority_class is AuthorityClass.SYSTEM
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is None
            and context.operation == "routing.recover"
        )
        if not context.mutation or not (service_create or system_recovery):
            raise RoutingError(RoutingErrorCode.INSUFFICIENT_SCOPE, context.request_id)
        if (
            not owner_id
            or len(owner_id) > 500
            or any(not " " <= character <= "~" for character in owner_id)
        ):
            raise ValueError("The routing owner identity is invalid.")
        try:
            uuid.UUID(request_id)
        except ValueError as error:
            raise RoutingError(
                RoutingErrorCode.NOT_FOUND, context.request_id
            ) from error
        claim_id = self._identity_factory()
        attempt_id = self._identity_factory()
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            request = connection.execute(
                """SELECT * FROM router.logical_requests
                   WHERE request_id = %s AND service_id = %s
                     AND workspace_id IS NOT DISTINCT FROM %s FOR UPDATE""",
                (request_id, context.scope.service_id, context.scope.workspace_id),
            ).fetchone()
            if request is None:
                raise RoutingError(RoutingErrorCode.NOT_FOUND, context.request_id)
            existing_claim = connection.execute(
                """SELECT * FROM router.routing_attempt_claims
                   WHERE request_row_id = %s FOR UPDATE""",
                (request["row_id"],),
            ).fetchone()
            if existing_claim is not None:
                snapshot = connection.execute(
                    """SELECT * FROM router.provider_route_execution_snapshots
                       WHERE id = %s""",
                    (existing_claim["route_snapshot_id"],),
                ).fetchone()
                assert snapshot is not None
                database_now = connection.execute(
                    "SELECT transaction_timestamp() AS value"
                ).fetchone()
                assert database_now is not None
                work = connection.execute(
                    """SELECT EXISTS (
                           SELECT 1 FROM router.routing_attempt_starts
                           WHERE claim_id = %s AND attempt_id = %s
                       ) AS started,
                       EXISTS (
                           SELECT 1 FROM router.routing_attempt_dispatches
                           WHERE claim_id = %s AND attempt_id = %s
                       ) AS dispatched,
                       (SELECT reservation.id::text
                        FROM router.logical_request_budget_sets AS budget_set
                        JOIN router.budget_candidate_reservations AS reservation
                          ON reservation.budget_set_id = budget_set.id
                        WHERE budget_set.request_row_id = %s
                          AND reservation.reservation_key = %s::text)
                          AS reservation_id""",
                    (
                        existing_claim["claim_id"],
                        existing_claim["attempt_id"],
                        existing_claim["claim_id"],
                        existing_claim["attempt_id"],
                        request["row_id"],
                        existing_claim["claim_id"],
                    ),
                ).fetchone()
                assert work is not None
                started = work["started"]
                dispatched = work["dispatched"]
                prestart_reservation_id = None if started else work["reservation_id"]
                controls = _controls(
                    connection, request, existing_claim["route_snapshot_id"]
                )
                execution_live = (
                    request["state"] == "running"
                    and not request["partial_output"]
                    and not request["committed_effect"]
                    and controls["credential_state"] == "active"
                    and controls["credential_revision_current"]
                    and controls["diagnostic_authorized"]
                )
                deadline_late = existing_claim["attempt_deadline"] <= database_now[
                    "value"
                ] + timedelta(milliseconds=100)
                recovery_only = dispatched or not execution_live or deadline_late
                recovery_failure = _recovery_failure(
                    request,
                    snapshot,
                    controls,
                    deadline_late=deadline_late,
                )
                if existing_claim["lease_expires_at"] > database_now["value"]:
                    if existing_claim["owner_id"] != owner_id:
                        raise RoutingError(RoutingErrorCode.BUSY, context.request_id)
                    return _plan(
                        request,
                        snapshot,
                        existing_claim,
                        started=started,
                        dispatched=dispatched,
                        recovery_only=recovery_only,
                        recovery_failure=recovery_failure,
                        prestart_reservation_id=prestart_reservation_id,
                    )
                if deadline_late and not started:
                    return _plan(
                        request,
                        snapshot,
                        existing_claim,
                        recovery_only=True,
                        recovery_failure=recovery_failure,
                        prestart_reservation_id=prestart_reservation_id,
                    )
                if not started and not execution_live:
                    return _plan(
                        request,
                        snapshot,
                        existing_claim,
                        recovery_only=True,
                        recovery_failure=recovery_failure,
                        prestart_reservation_id=prestart_reservation_id,
                    )
                recovered = connection.execute(
                    """UPDATE router.routing_attempt_claims
                       SET owner_id = %s, claim_generation = claim_generation + 1,
                           claimed_at = transaction_timestamp(),
                           lease_expires_at = transaction_timestamp() + interval '30 seconds'
                       WHERE request_row_id = %s AND claim_generation = %s
                       RETURNING *""",
                    (
                        owner_id,
                        request["row_id"],
                        existing_claim["claim_generation"],
                    ),
                ).fetchone()
                if recovered is None:
                    raise RoutingError(
                        RoutingErrorCode.CLAIM_CONFLICT, context.request_id
                    )
                return _plan(
                    request,
                    snapshot,
                    recovered,
                    started=started,
                    dispatched=dispatched,
                    recovery_only=recovery_only,
                    recovery_failure=recovery_failure,
                )
            request_terminal = connection.execute(
                """SELECT * FROM router.routing_request_terminal_decisions
                   WHERE request_row_id = %s""",
                (request["row_id"],),
            ).fetchone()
            if request_terminal is not None:
                snapshot = connection.execute(
                    """SELECT * FROM router.provider_route_execution_snapshots
                       WHERE id = %s""",
                    (request_terminal["route_snapshot_id"],),
                ).fetchone()
                assert snapshot is not None
                return _plan(
                    request,
                    snapshot,
                    _decision_claim(request_terminal),
                    recovery_only=True,
                    recovery_failure=_decision_failure(request_terminal),
                    request_terminal=True,
                )
            if request["state"] != "running":
                raise RoutingError(RoutingErrorCode.NO_CANDIDATE, context.request_id)
            prior = connection.execute(
                """SELECT * FROM router.routing_candidate_decisions
                   WHERE request_row_id = %s ORDER BY decision_sequence DESC LIMIT 1""",
                (request["row_id"],),
            ).fetchone()
            if prior is not None and prior["fallback_decision"] != "next_candidate":
                raise RoutingError(RoutingErrorCode.NO_CANDIDATE, context.request_id)
            attempt_count = connection.execute(
                "SELECT count(*) AS value FROM router.provider_attempts WHERE request_row_id = %s",
                (request["row_id"],),
            ).fetchone()
            assert attempt_count is not None
            if attempt_count["value"] >= 8:
                raise RoutingError(RoutingErrorCode.NO_CANDIDATE, context.request_id)
            if prior is None:
                ordinal = 1
            elif (
                prior["affected_scope"] == "attempt"
                and connection.execute(
                    "SELECT 1 FROM router.provider_attempts WHERE id = %s",
                    (prior["attempt_id"],),
                ).fetchone()
                is not None
            ):
                ordinal = prior["candidate_ordinal"]
            else:
                next_row = connection.execute(
                    """SELECT candidate_ordinal
                       FROM router.provider_route_execution_snapshots AS snapshot
                       WHERE snapshot.request_row_id = %s
                         AND snapshot.candidate_ordinal > %s
                         AND NOT EXISTS (
                             SELECT 1 FROM router.routing_candidate_decisions AS exclusion
                             WHERE exclusion.request_row_id = snapshot.request_row_id
                               AND (
                                   exclusion.affected_scope = 'logical_request'
                                   OR (exclusion.affected_scope = 'provider_model_route'
                                       AND exclusion.affected_scope_id = snapshot.provider_model_route_id::text)
                                   OR (exclusion.affected_scope = 'provider_instance'
                                       AND exclusion.affected_scope_id = snapshot.provider_instance_id::text)
                                   OR (exclusion.affected_scope = 'credential'
                                       AND exclusion.affected_scope_id = snapshot.credential_id::text)
                                   OR (exclusion.affected_scope = 'assignment_candidate'
                                       AND exclusion.affected_scope_id = COALESCE(
                                           %s::text, 'exact:' || snapshot.provider_model_route_id::text
                                       ) || ':' ||
                                           snapshot.candidate_ordinal::text)
                               )
                         ) ORDER BY candidate_ordinal LIMIT 1""",
                    (
                        request["row_id"],
                        prior["candidate_ordinal"],
                        request["assignment_id"],
                    ),
                ).fetchone()
                if next_row is None:
                    raise RoutingError(
                        RoutingErrorCode.NO_CANDIDATE, context.request_id
                    )
                ordinal = next_row["candidate_ordinal"]
            snapshot = connection.execute(
                """SELECT * FROM router.provider_route_execution_snapshots
                   WHERE request_row_id = %s AND candidate_ordinal = %s""",
                (request["row_id"], ordinal),
            ).fetchone()
            if snapshot is None:
                raise RoutingError(RoutingErrorCode.NO_CANDIDATE, context.request_id)
            database_now = connection.execute(
                "SELECT transaction_timestamp() AS value"
            ).fetchone()
            assert database_now is not None
            if request["admitted_at"] + timedelta(minutes=15) <= database_now[
                "value"
            ] + timedelta(milliseconds=100):
                logical_deadline = request["admitted_at"] + timedelta(minutes=15)
                terminal = connection.execute(
                    """INSERT INTO router.routing_request_terminal_decisions (
                           request_row_id, decision_id, attempt_id, claim_id,
                           attempt_number, candidate_ordinal, route_snapshot_id,
                           connect_timeout_ms, first_byte_timeout_ms, idle_timeout_ms,
                           execution_timeout_ms, logical_deadline, attempt_deadline,
                           attempt_state, normalized_error_class, affected_scope,
                           affected_scope_id, fallback_decision, safe_provider_code,
                           redacted_evidence, occurred_at
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,100,100,100,100,%s,%s,
                                 'failed','timeout','logical_request',%s,'stop_request',
                                 NULL,jsonb_build_object(
                                     'provider_status', NULL, 'retry_after_ms', NULL,
                                     'detail_code', 'logical_deadline'
                                 ),
                                 transaction_timestamp())
                       RETURNING *""",
                    (
                        request["row_id"],
                        self._identity_factory(),
                        attempt_id,
                        claim_id,
                        attempt_count["value"] + 1,
                        ordinal,
                        snapshot["id"],
                        logical_deadline,
                        logical_deadline,
                        str(request["request_id"]),
                    ),
                ).fetchone()
                assert terminal is not None
                return _plan(
                    request,
                    snapshot,
                    _decision_claim(terminal),
                    recovery_only=True,
                    recovery_failure=_decision_failure(terminal),
                    request_terminal=True,
                )
            controls = _controls(connection, request, snapshot["id"])
            execution_live = (
                controls["credential_state"] == "active"
                and controls["credential_revision_current"]
                and controls["diagnostic_authorized"]
            )
            attempt_number = attempt_count["value"] + 1
            row = connection.execute(
                """INSERT INTO router.routing_attempt_claims (
                       request_row_id, claim_id, claim_generation, owner_id, attempt_id,
                       attempt_number, candidate_ordinal, assignment_revision_id,
                       route_snapshot_id, candidate_policy, connect_timeout_ms,
                       first_byte_timeout_ms, idle_timeout_ms, execution_timeout_ms,
                       logical_deadline, attempt_deadline, claimed_at, lease_expires_at
                   ) VALUES (
                       %s, %s, 1, %s, %s, %s, %s, %s, %s, %s,
                       LEAST(10000, LEAST(%s, floor(extract(epoch FROM
                           (%s + interval '15 minutes' - transaction_timestamp())) * 1000)::integer)),
                       LEAST(30000, LEAST(%s, floor(extract(epoch FROM
                           (%s + interval '15 minutes' - transaction_timestamp())) * 1000)::integer)),
                       LEAST(30000, LEAST(%s, floor(extract(epoch FROM
                           (%s + interval '15 minutes' - transaction_timestamp())) * 1000)::integer)),
                       LEAST(%s, floor(extract(epoch FROM
                           (%s + interval '15 minutes' - transaction_timestamp())) * 1000)::integer),
                       %s + interval '15 minutes',
                       transaction_timestamp() + LEAST(%s, floor(extract(epoch FROM
                           (%s + interval '15 minutes' - transaction_timestamp())) * 1000)::integer)
                           * interval '1 millisecond',
                       transaction_timestamp(), transaction_timestamp() + interval '30 seconds'
                   ) RETURNING *""",
                (
                    request["row_id"],
                    claim_id,
                    owner_id,
                    attempt_id,
                    attempt_number,
                    ordinal,
                    snapshot["assignment_revision_id"],
                    snapshot["id"],
                    Jsonb(snapshot["candidate_policy"]),
                    snapshot["attempt_timeout_ms"],
                    request["admitted_at"],
                    snapshot["attempt_timeout_ms"],
                    request["admitted_at"],
                    snapshot["attempt_timeout_ms"],
                    request["admitted_at"],
                    snapshot["attempt_timeout_ms"],
                    request["admitted_at"],
                    request["admitted_at"],
                    snapshot["attempt_timeout_ms"],
                    request["admitted_at"],
                ),
            ).fetchone()
            assert row is not None
            return _plan(
                request,
                snapshot,
                row,
                recovery_only=not execution_live,
                recovery_failure=_recovery_failure(
                    request, snapshot, controls, deadline_late=False
                ),
            )

    def start(self, plan: AttemptPlan, *, budget_reservation_id: str) -> None:
        """Bind one durable budget reservation before provider work starts."""
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            replay = connection.execute(
                "SELECT 1 FROM router.routing_attempt_starts WHERE attempt_id = %s",
                (plan.attempt_id,),
            ).fetchone()
            if replay is not None:
                if _started(connection, plan, budget_reservation_id):
                    return
                raise RoutingError(RoutingErrorCode.CLAIM_CONFLICT, plan.request_id)
            connection.execute(
                """INSERT INTO router.routing_attempt_starts (
                       attempt_id, request_row_id, claim_id, claim_generation,
                       candidate_ordinal, route_snapshot_id, budget_reservation_id,
                       reservation_key, started_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,transaction_timestamp())""",
                (
                    plan.attempt_id,
                    plan.request_row_id,
                    plan.claim_id,
                    plan.claim_generation,
                    plan.candidate_ordinal,
                    plan.route_snapshot_id,
                    budget_reservation_id,
                    plan.reservation_key,
                ),
            )
            connection.execute(
                """INSERT INTO router.provider_attempts (
                       id, request_row_id, service_id, workspace_id, attempt_number,
                       provider_model_route_id, route_generation, assignment_revision_id,
                       price_version_id, route_snapshot_id, candidate_ordinal,
                       provider_instance_id, provider_instance_generation, credential_id,
                       credential_generation, connect_timeout_ms, first_byte_timeout_ms,
                       idle_timeout_ms, execution_timeout_ms, logical_deadline,
                       attempt_deadline, budget_reservation_id, state, started_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                             'started', transaction_timestamp())""",
                (
                    plan.attempt_id,
                    plan.request_row_id,
                    plan.service_id,
                    plan.workspace_id,
                    plan.attempt_number,
                    plan.provider_model_route_id,
                    plan.route_generation,
                    plan.assignment_revision,
                    plan.price_version_id,
                    plan.route_snapshot_id,
                    plan.candidate_ordinal,
                    plan.provider_instance_id,
                    plan.provider_instance_generation,
                    plan.credential_id,
                    plan.credential_generation,
                    plan.timeouts.connect_ms,
                    plan.timeouts.first_byte_ms,
                    plan.timeouts.idle_ms,
                    plan.timeouts.execution_ms,
                    plan.logical_deadline,
                    plan.attempt_deadline,
                    budget_reservation_id,
                ),
            )

    def started(self, plan: AttemptPlan, *, budget_reservation_id: str) -> bool:
        """Prove the complete durable start after an unknown commit result."""
        with psycopg.connect(self._database_url) as connection:
            return _started(connection, plan, budget_reservation_id)

    def reject_before_start(
        self,
        plan: AttemptPlan,
        failure: AttemptFailure,
        decision: FallbackDecision,
        *,
        now: datetime,
    ) -> None:
        """Record one safe candidate skip without a provider attempt."""
        del now
        attempt_state = (
            AttemptOutcome.CANCELLED.value
            if decision is FallbackDecision.CANCELLED
            else AttemptOutcome.FAILED.value
        )
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            replay = connection.execute(
                """SELECT claim_id::text, claim_generation, attempt_number,
                          candidate_ordinal, route_snapshot_id::text,
                          connect_timeout_ms, first_byte_timeout_ms, idle_timeout_ms,
                          execution_timeout_ms, logical_deadline, attempt_deadline,
                          attempt_state, normalized_error_class, affected_scope,
                          affected_scope_id, fallback_decision, safe_provider_code,
                          redacted_evidence
                   FROM router.routing_candidate_decisions WHERE attempt_id = %s""",
                (plan.attempt_id,),
            ).fetchone()
            if replay is not None:
                if replay == (
                    plan.claim_id,
                    plan.claim_generation,
                    plan.attempt_number,
                    plan.candidate_ordinal,
                    plan.route_snapshot_id,
                    plan.timeouts.connect_ms,
                    plan.timeouts.first_byte_ms,
                    plan.timeouts.idle_ms,
                    plan.timeouts.execution_ms,
                    plan.logical_deadline,
                    plan.attempt_deadline,
                    attempt_state,
                    failure.error.error_class.value,
                    failure.error.affected_scope.value,
                    failure.affected_scope_id,
                    decision.value,
                    failure.error.safe_provider_code,
                    failure.evidence.document(),
                ):
                    return
                raise RoutingError(RoutingErrorCode.CLAIM_CONFLICT, plan.request_id)
            sequence_row = connection.execute(
                "SELECT COALESCE(max(decision_sequence), 0) + 1 AS value FROM router.routing_candidate_decisions WHERE request_row_id=%s",
                (plan.request_row_id,),
            ).fetchone()
            assert sequence_row is not None
            sequence = sequence_row[0]
            connection.execute(
                """INSERT INTO router.routing_candidate_decisions (
                       decision_id, request_row_id, decision_sequence, attempt_id, claim_id,
                       claim_generation, attempt_number, candidate_ordinal, route_snapshot_id,
                       connect_timeout_ms, first_byte_timeout_ms, idle_timeout_ms,
                       execution_timeout_ms, logical_deadline, attempt_deadline, attempt_state,
                       normalized_error_class, affected_scope, affected_scope_id,
                       fallback_decision, safe_provider_code, redacted_evidence, occurred_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                             transaction_timestamp())""",
                (
                    self._identity_factory(),
                    plan.request_row_id,
                    sequence,
                    plan.attempt_id,
                    plan.claim_id,
                    plan.claim_generation,
                    plan.attempt_number,
                    plan.candidate_ordinal,
                    plan.route_snapshot_id,
                    plan.timeouts.connect_ms,
                    plan.timeouts.first_byte_ms,
                    plan.timeouts.idle_ms,
                    plan.timeouts.execution_ms,
                    plan.logical_deadline,
                    plan.attempt_deadline,
                    attempt_state,
                    failure.error.error_class.value,
                    failure.error.affected_scope.value,
                    failure.affected_scope_id,
                    decision.value,
                    failure.error.safe_provider_code,
                    Jsonb(failure.evidence.document()),
                ),
            )
            connection.execute(
                "DELETE FROM router.routing_attempt_claims WHERE claim_id=%s",
                (plan.claim_id,),
            )

    def dispatch(self, plan: AttemptPlan, *, owner_id: str) -> bool:
        """Record the no-repeat provider dispatch boundary."""
        with (
            psycopg.connect(self._database_url) as connection,
            connection.transaction(),
        ):
            existing = connection.execute(
                """SELECT dispatch.claim_id::text, dispatch.claim_generation,
                          dispatch.owner_id,
                          claim.claim_generation, claim.owner_id
                   FROM router.routing_attempt_dispatches AS dispatch
                   LEFT JOIN router.routing_attempt_claims AS claim
                     ON claim.claim_id = dispatch.claim_id
                    AND claim.attempt_id = dispatch.attempt_id
                   WHERE dispatch.attempt_id = %s""",
                (plan.attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing[0] == plan.claim_id
                    and existing[3] == plan.claim_generation
                    and existing[4] == owner_id
                ):
                    return False
                raise RoutingError(RoutingErrorCode.CLAIM_CONFLICT, plan.request_id)
            connection.execute(
                """INSERT INTO router.routing_attempt_dispatches (
                       attempt_id, claim_id, claim_generation, owner_id, dispatched_at
                   ) VALUES (%s,%s,%s,%s,transaction_timestamp())""",
                (
                    plan.attempt_id,
                    plan.claim_id,
                    plan.claim_generation,
                    owner_id,
                ),
            )
            return True

    def finish(
        self,
        plan: AttemptPlan,
        result: AdapterResult,
        decision: FallbackDecision,
        *,
        now: datetime,
    ) -> AdapterResult:
        """Commit the terminal attempt and matching routing decision together."""
        del now
        failure = result.failure
        with (
            psycopg.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            updated = connection.execute(
                """UPDATE router.provider_attempts SET state=%s,
                       finished_at=transaction_timestamp(),
                       normalized_error_class=%s, affected_scope=%s, affected_scope_id=%s,
                       retry_decision=%s, safe_provider_code=%s, redacted_evidence=%s
                   WHERE id=%s AND state='started'""",
                (
                    result.outcome.value,
                    None if failure is None else failure.error.error_class.value,
                    None if failure is None else failure.error.affected_scope.value,
                    None if failure is None else failure.affected_scope_id,
                    decision.value,
                    None if failure is None else failure.error.safe_provider_code,
                    None if failure is None else Jsonb(failure.evidence.document()),
                    plan.attempt_id,
                ),
            )
            if updated.rowcount == 0:
                _store_usage(connection, plan, result)
                terminal = connection.execute(
                    """SELECT attempt.state::text AS state,
                              attempt.normalized_error_class,
                              attempt.affected_scope, attempt.affected_scope_id,
                              attempt.safe_provider_code, attempt.redacted_evidence,
                              decision.fallback_decision,
                              COALESCE(usage.usage_components, '[]'::jsonb)
                                AS usage_components
                       FROM router.provider_attempts AS attempt
                       JOIN router.routing_candidate_decisions AS decision
                         ON decision.attempt_id = attempt.id
                       JOIN router.routing_attempt_starts AS attempt_start
                         ON attempt_start.attempt_id = attempt.id
                       LEFT JOIN router.routing_attempt_usage_reports AS usage
                         ON usage.attempt_id = attempt.id
                       WHERE attempt.id = %s AND attempt.state <> 'started'
                         AND attempt.request_row_id = %s
                         AND attempt.service_id = %s
                         AND attempt.workspace_id IS NOT DISTINCT FROM %s
                         AND attempt.attempt_number = %s
                         AND attempt.provider_model_route_id = %s
                         AND attempt.route_generation = %s
                         AND attempt.assignment_revision_id = %s
                         AND attempt.price_version_id = %s
                         AND attempt.route_snapshot_id = %s
                         AND attempt.candidate_ordinal = %s
                         AND attempt.provider_instance_id = %s
                         AND attempt.provider_instance_generation = %s
                         AND attempt.credential_id = %s
                         AND attempt.credential_generation = %s
                         AND attempt.connect_timeout_ms = %s
                         AND attempt.first_byte_timeout_ms = %s
                         AND attempt.idle_timeout_ms = %s
                         AND attempt.execution_timeout_ms = %s
                         AND attempt.logical_deadline = %s
                         AND attempt.attempt_deadline = %s
                         AND attempt_start.request_row_id = %s
                         AND attempt_start.claim_id = %s
                         AND attempt_start.claim_generation <= %s
                         AND attempt_start.candidate_ordinal = %s
                         AND attempt_start.route_snapshot_id = %s
                         AND attempt_start.reservation_key = %s
                         AND decision.request_row_id = %s
                         AND decision.attempt_number = %s
                         AND decision.candidate_ordinal = %s
                         AND decision.route_snapshot_id = %s
                         AND decision.attempt_state = attempt.state::text
                         AND decision.normalized_error_class IS NOT DISTINCT FROM
                             attempt.normalized_error_class
                         AND decision.affected_scope IS NOT DISTINCT FROM
                             attempt.affected_scope
                         AND decision.affected_scope_id IS NOT DISTINCT FROM
                             attempt.affected_scope_id
                         AND decision.fallback_decision IS NOT DISTINCT FROM
                             attempt.retry_decision
                         AND decision.safe_provider_code IS NOT DISTINCT FROM
                             attempt.safe_provider_code
                         AND decision.redacted_evidence IS NOT DISTINCT FROM
                             attempt.redacted_evidence
                         AND decision.occurred_at = attempt.finished_at""",
                    (
                        plan.attempt_id,
                        plan.request_row_id,
                        plan.service_id,
                        plan.workspace_id,
                        plan.attempt_number,
                        plan.provider_model_route_id,
                        plan.route_generation,
                        plan.assignment_revision,
                        plan.price_version_id,
                        plan.route_snapshot_id,
                        plan.candidate_ordinal,
                        plan.provider_instance_id,
                        plan.provider_instance_generation,
                        plan.credential_id,
                        plan.credential_generation,
                        plan.timeouts.connect_ms,
                        plan.timeouts.first_byte_ms,
                        plan.timeouts.idle_ms,
                        plan.timeouts.execution_ms,
                        plan.logical_deadline,
                        plan.attempt_deadline,
                        plan.request_row_id,
                        plan.claim_id,
                        plan.claim_generation,
                        plan.candidate_ordinal,
                        plan.route_snapshot_id,
                        plan.reservation_key,
                        plan.request_row_id,
                        plan.attempt_number,
                        plan.candidate_ordinal,
                        plan.route_snapshot_id,
                    ),
                ).fetchone()
                if terminal is not None:
                    durable_result = _terminal_result(terminal)
                    if durable_result == result or durable_result.outcome in {
                        AttemptOutcome.CANCELLED,
                        AttemptOutcome.UNCERTAIN,
                    }:
                        connection.execute(
                            """DELETE FROM router.routing_attempt_claims
                               WHERE request_row_id = %s AND attempt_id = %s
                                 AND claim_id = %s""",
                            (
                                plan.request_row_id,
                                plan.attempt_id,
                                plan.claim_id,
                            ),
                        )
                        return durable_result
                raise RoutingError(RoutingErrorCode.CLAIM_CONFLICT, plan.request_id)
            sequence_row = connection.execute(
                "SELECT COALESCE(max(decision_sequence), 0) + 1 AS value FROM router.routing_candidate_decisions WHERE request_row_id=%s",
                (plan.request_row_id,),
            ).fetchone()
            assert sequence_row is not None
            sequence = sequence_row["value"]
            connection.execute(
                """INSERT INTO router.routing_candidate_decisions (
                       decision_id, request_row_id, decision_sequence, attempt_id, claim_id,
                       claim_generation, attempt_number, candidate_ordinal, route_snapshot_id,
                       connect_timeout_ms, first_byte_timeout_ms, idle_timeout_ms,
                       execution_timeout_ms, logical_deadline, attempt_deadline, attempt_state,
                       normalized_error_class, affected_scope, affected_scope_id,
                       fallback_decision, safe_provider_code, redacted_evidence, occurred_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                             transaction_timestamp())""",
                (
                    self._identity_factory(),
                    plan.request_row_id,
                    sequence,
                    plan.attempt_id,
                    plan.claim_id,
                    plan.claim_generation,
                    plan.attempt_number,
                    plan.candidate_ordinal,
                    plan.route_snapshot_id,
                    plan.timeouts.connect_ms,
                    plan.timeouts.first_byte_ms,
                    plan.timeouts.idle_ms,
                    plan.timeouts.execution_ms,
                    plan.logical_deadline,
                    plan.attempt_deadline,
                    result.outcome.value,
                    None if failure is None else failure.error.error_class.value,
                    None if failure is None else failure.error.affected_scope.value,
                    None if failure is None else failure.affected_scope_id,
                    decision.value,
                    None if failure is None else failure.error.safe_provider_code,
                    None if failure is None else Jsonb(failure.evidence.document()),
                ),
            )
            connection.execute(
                "DELETE FROM router.routing_attempt_claims WHERE claim_id=%s",
                (plan.claim_id,),
            )
            _store_usage(connection, plan, result)
            return result


def _recovery_failure(
    request: dict[str, Any],
    snapshot: dict[str, Any],
    controls: dict[str, Any],
    *,
    deadline_late: bool,
) -> AttemptFailure | None:
    """Return the smallest safe failure that forced recovery-only work."""
    request_id = str(request["request_id"])
    if deadline_late:
        return _failure(
            TerminalErrorClass.TIMEOUT,
            ErrorScope.LOGICAL_REQUEST,
            request_id,
            "logical_deadline",
        )
    if request["state"] == "cancel_requested":
        return _failure(
            TerminalErrorClass.CANCELLED,
            ErrorScope.LOGICAL_REQUEST,
            request_id,
            "cancel_requested",
        )
    if (
        request["state"] != "running"
        or request["partial_output"]
        or request["committed_effect"]
    ):
        return _failure(
            TerminalErrorClass.POLICY,
            ErrorScope.LOGICAL_REQUEST,
            request_id,
            "request_control_changed",
        )
    if (
        controls["credential_state"] != "active"
        or not controls["credential_revision_current"]
    ):
        return _failure(
            TerminalErrorClass.AUTHENTICATION,
            ErrorScope.CREDENTIAL,
            str(snapshot["credential_id"]),
            "credential_disabled",
        )
    if not controls["diagnostic_authorized"]:
        return _failure(
            TerminalErrorClass.POLICY,
            ErrorScope.LOGICAL_REQUEST,
            request_id,
            "diagnostic_authorization_changed",
        )
    return None


def _controls(
    connection: psycopg.Connection[dict[str, Any]],
    request: dict[str, Any],
    route_snapshot_id: uuid.UUID,
) -> dict[str, Any]:
    """Return urgent execution controls without applying normal config changes."""
    controls = connection.execute(
        """SELECT route.state::text AS route_state,
                  instance.state::text AS instance_state,
                  credential.state::text AS credential_state,
                  credential.generation = snapshot.credential_generation
                    AND credential.current_revision = snapshot.credential_revision_id
                    AS credential_revision_current,
                  router.provider_route_is_eligible(route.id, %s) AS route_eligible,
                  (%s IS NOT NULL OR EXISTS (
                      SELECT 1
                      FROM router.diagnostic_route_authorizations AS diagnostic_use
                      WHERE diagnostic_use.request_id = %s
                        AND diagnostic_use.service_id = %s
                        AND diagnostic_use.workspace_id IS NOT DISTINCT FROM %s
                        AND diagnostic_use.exact_route_id = snapshot.provider_model_route_id
                        AND diagnostic_use.route_configuration_revision_id =
                            snapshot.route_configuration_revision_id
                  )) AS diagnostic_authorized
           FROM router.provider_route_execution_snapshots AS snapshot
           JOIN router.provider_model_routes AS route
             ON route.id = snapshot.provider_model_route_id
           JOIN router.provider_instances AS instance
             ON instance.id = snapshot.provider_instance_id
           JOIN router.encrypted_credentials AS credential
             ON credential.id = snapshot.credential_id
           WHERE snapshot.id = %s""",
        (
            request["service_id"],
            request["assignment_id"],
            request["request_id"],
            request["service_id"],
            request["workspace_id"],
            route_snapshot_id,
        ),
    ).fetchone()
    assert controls is not None
    return controls


def _require_routing_authority(context: RequestContext) -> None:
    service_create = (
        context.actor_kind is PrincipalKind.SERVICE
        and context.actor_id == context.scope.service_id
        and context.authority_class is AuthorityClass.SERVICE
        and context.operation in {"model.create", "tool.create"}
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.DATA_PLANE
    )
    system_recovery = (
        context.actor_kind is PrincipalKind.SYSTEM
        and context.authority_class is AuthorityClass.SYSTEM
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is None
        and context.operation == "routing.recover"
    )
    if not context.mutation or not (service_create or system_recovery):
        raise RoutingError(RoutingErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _failure(
    error_class: TerminalErrorClass,
    scope: ErrorScope,
    scope_id: str,
    detail_code: str,
) -> AttemptFailure:
    return AttemptFailure(
        TerminalError(
            error_class,
            scope,
            "The provider attempt cannot continue.",
        ),
        scope_id,
        SafeFailureEvidence(detail_code=detail_code),
    )


def _terminal_result(row: dict[str, Any]) -> AdapterResult:
    """Rebuild the closed durable terminal result for safe replay."""
    outcome = AttemptOutcome(row["state"])
    usage = tuple(
        UsageComponent(UsageUnit(component["unit"]), Decimal(component["quantity"]))
        for component in row.get("usage_components", [])
    )
    if outcome is AttemptOutcome.SUCCEEDED:
        return AdapterResult(outcome, usage=usage)
    evidence = row["redacted_evidence"]
    if not isinstance(evidence, dict):
        raise TypeError("Durable routing evidence is invalid.")
    failure = AttemptFailure(
        TerminalError(
            TerminalErrorClass(row["normalized_error_class"]),
            ErrorScope(row["affected_scope"]),
            "The provider attempt did not complete.",
            row["safe_provider_code"],
        ),
        row["affected_scope_id"],
        SafeFailureEvidence(
            provider_status=evidence.get("provider_status"),
            retry_after_ms=evidence.get("retry_after_ms"),
            detail_code=evidence.get("detail_code"),
        ),
    )
    return AdapterResult(outcome, failure, usage)


def _decision_failure(row: dict[str, Any]) -> AttemptFailure:
    """Rebuild safe failure data from an append-only routing decision."""
    evidence = row["redacted_evidence"]
    if not isinstance(evidence, dict):
        raise TypeError("Durable routing evidence is invalid.")
    return AttemptFailure(
        TerminalError(
            TerminalErrorClass(row["normalized_error_class"]),
            ErrorScope(row["affected_scope"]),
            "The provider attempt did not complete.",
            row["safe_provider_code"],
        ),
        row["affected_scope_id"],
        SafeFailureEvidence(
            provider_status=evidence.get("provider_status"),
            retry_after_ms=evidence.get("retry_after_ms"),
            detail_code=evidence.get("detail_code"),
        ),
    )


def _decision_result(row: dict[str, Any]) -> AdapterResult:
    """Rebuild a prestart or request-level durable result."""
    return AdapterResult(AttemptOutcome(row["attempt_state"]), _decision_failure(row))


def _decision_claim(row: dict[str, Any]) -> dict[str, Any]:
    """Expose stored fixed plan values through the normal plan constructor."""
    return {
        "claim_id": row["claim_id"],
        "claim_generation": row.get("claim_generation") or 1,
        "attempt_id": row["attempt_id"],
        "attempt_number": row["attempt_number"],
        "candidate_ordinal": row["candidate_ordinal"],
        "assignment_revision_id": None,
        "route_snapshot_id": row["route_snapshot_id"],
        "candidate_policy": {},
        "connect_timeout_ms": row["connect_timeout_ms"],
        "first_byte_timeout_ms": row["first_byte_timeout_ms"],
        "idle_timeout_ms": row["idle_timeout_ms"],
        "execution_timeout_ms": row["execution_timeout_ms"],
        "logical_deadline": row["logical_deadline"],
        "attempt_deadline": row["attempt_deadline"],
    }


def _store_usage(
    connection: psycopg.Connection[dict[str, Any]],
    plan: AttemptPlan,
    result: AdapterResult,
) -> None:
    if not result.usage:
        return
    usage_document = [
        {
            "unit": component.unit.value,
            "quantity": format(component.quantity.normalize(), "f"),
        }
        for component in result.usage
    ]
    connection.execute(
        """INSERT INTO router.routing_attempt_usage_reports (
               attempt_id, usage_components, reported_at
           ) VALUES (%s,%s,transaction_timestamp())
           ON CONFLICT (attempt_id) DO NOTHING""",
        (plan.attempt_id, Jsonb(usage_document)),
    )
    stored = connection.execute(
        """SELECT usage_components
           FROM router.routing_attempt_usage_reports WHERE attempt_id = %s""",
        (plan.attempt_id,),
    ).fetchone()
    if stored is None or stored["usage_components"] != usage_document:
        raise RoutingError(RoutingErrorCode.CLAIM_CONFLICT, plan.request_id)


def _started(
    connection: psycopg.Connection[Any],
    plan: AttemptPlan,
    budget_reservation_id: str,
) -> bool:
    """Return true only for the complete expected durable start."""
    row = connection.execute(
        """SELECT 1
           FROM router.routing_attempt_starts AS attempt_start
           JOIN router.provider_attempts AS attempt
             ON attempt.id = attempt_start.attempt_id
           JOIN router.routing_attempt_claims AS claim
             ON claim.claim_id = attempt_start.claim_id
            AND claim.attempt_id = attempt_start.attempt_id
            AND claim.request_row_id = attempt_start.request_row_id
           WHERE attempt.id = %s
             AND attempt.request_row_id = %s
             AND attempt.service_id = %s
             AND attempt.workspace_id IS NOT DISTINCT FROM %s
             AND attempt.attempt_number = %s
             AND attempt.provider_model_route_id = %s
             AND attempt.route_generation = %s
             AND attempt.assignment_revision_id = %s
             AND attempt.price_version_id = %s
             AND attempt.route_snapshot_id = %s
             AND attempt.candidate_ordinal = %s
             AND attempt.provider_instance_id = %s
             AND attempt.provider_instance_generation = %s
             AND attempt.credential_id = %s
             AND attempt.credential_generation = %s
             AND attempt.connect_timeout_ms = %s
             AND attempt.first_byte_timeout_ms = %s
             AND attempt.idle_timeout_ms = %s
             AND attempt.execution_timeout_ms = %s
             AND attempt.logical_deadline = %s
             AND attempt.attempt_deadline = %s
             AND attempt.budget_reservation_id = %s
             AND attempt_start.request_row_id = %s
             AND attempt_start.claim_id = %s
             AND attempt_start.claim_generation <= %s
             AND claim.claim_generation = %s
             AND claim.request_row_id = %s
             AND claim.attempt_number = %s
             AND claim.candidate_ordinal = %s
             AND claim.route_snapshot_id = %s
             AND attempt_start.candidate_ordinal = %s
             AND attempt_start.route_snapshot_id = %s
             AND attempt_start.budget_reservation_id = %s
             AND attempt_start.reservation_key = %s""",
        (
            plan.attempt_id,
            plan.request_row_id,
            plan.service_id,
            plan.workspace_id,
            plan.attempt_number,
            plan.provider_model_route_id,
            plan.route_generation,
            plan.assignment_revision,
            plan.price_version_id,
            plan.route_snapshot_id,
            plan.candidate_ordinal,
            plan.provider_instance_id,
            plan.provider_instance_generation,
            plan.credential_id,
            plan.credential_generation,
            plan.timeouts.connect_ms,
            plan.timeouts.first_byte_ms,
            plan.timeouts.idle_ms,
            plan.timeouts.execution_ms,
            plan.logical_deadline,
            plan.attempt_deadline,
            budget_reservation_id,
            plan.request_row_id,
            plan.claim_id,
            plan.claim_generation,
            plan.claim_generation,
            plan.request_row_id,
            plan.attempt_number,
            plan.candidate_ordinal,
            plan.route_snapshot_id,
            plan.candidate_ordinal,
            plan.route_snapshot_id,
            budget_reservation_id,
            plan.reservation_key,
        ),
    ).fetchone()
    return row is not None


def _plan(  # noqa: PLR0913
    request: dict[str, Any],
    snapshot: dict[str, Any],
    claim: dict[str, Any],
    *,
    started: bool = False,
    dispatched: bool = False,
    recovery_only: bool = False,
    recovery_failure: AttemptFailure | None = None,
    prestart_reservation_id: str | None = None,
    request_terminal: bool = False,
) -> AttemptPlan:
    prices = tuple(
        PriceComponent(
            UsageUnit(item["unit"]),
            Decimal(item["price"]),
            item["currency"],
            item["raw_source_value"],
            Decimal(item["unit_quantity"]),
        )
        for item in snapshot["typed_prices"]
    )
    return AttemptPlan(
        str(claim["claim_id"]),
        claim["claim_generation"],
        str(request["request_id"]),
        str(request["row_id"]),
        str(request["service_id"]),
        None if request["workspace_id"] is None else str(request["workspace_id"]),
        str(claim["attempt_id"]),
        claim["attempt_number"],
        claim["candidate_ordinal"],
        None if request["assignment_id"] is None else str(request["assignment_id"]),
        str(snapshot["assignment_revision_id"]),
        str(snapshot["id"]),
        bytes(snapshot["content_sha256"]),
        str(snapshot["route_configuration_revision_id"]),
        str(snapshot["provider_model_route_id"]),
        snapshot["route_generation"],
        str(snapshot["provider_instance_id"]),
        snapshot["provider_instance_generation"],
        str(snapshot["credential_id"]),
        snapshot["credential_generation"],
        str(snapshot["price_version_id"]),
        snapshot["adapter_type_id"],
        snapshot["endpoint_origin"],
        snapshot["wire_model"],
        frozenset(snapshot["capabilities"]),
        snapshot["candidate_policy"],
        snapshot["instance_settings"],
        snapshot["route_settings"],
        prices,
        AttemptTimeouts(
            claim["connect_timeout_ms"],
            claim["first_byte_timeout_ms"],
            claim["idle_timeout_ms"],
            claim["execution_timeout_ms"],
        ),
        claim["logical_deadline"],
        claim["attempt_deadline"],
        request["assignment_id"] is None,
        request["partial_output"],
        request["committed_effect"],
        started,
        dispatched,
        recovery_only,
        recovery_failure,
        prestart_reservation_id,
        request_terminal,
    )

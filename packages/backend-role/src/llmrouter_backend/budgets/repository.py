"""Transactional PostgreSQL hierarchical budget enforcement."""
# ruff: noqa: C901, E501, EM101, PLR0912, PLR0913, PLR0915, PLR2004, TRY003

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from llmrouter_backend.accounting.model import currency_code, exact_decimal
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets.errors import BudgetError, BudgetErrorCode
from llmrouter_backend.budgets.model import (
    BudgetCandidateKind,
    BudgetLimit,
    BudgetScopeKind,
    BudgetTarget,
    EnforcementState,
    EnforcementSummary,
    HostCeiling,
    Money,
    ReservationResult,
    ReservationState,
    ResetPeriod,
    SignedMoney,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection
    from psycopg.rows import DictRow


@dataclass(frozen=True, slots=True)
class _ScopeRow:
    scope_id: str
    kind: BudgetScopeKind
    currency: str
    limit: Decimal
    warning: Decimal | None
    reset_period: ResetPeriod
    revision: str


@dataclass(frozen=True, slots=True)
class _Balance:
    reserved: Decimal
    used: Decimal
    corrected: Decimal

    @property
    def consumed(self) -> Decimal:
        return self.reserved + max(self.used + self.corrected, Decimal(0))


class PostgresBudgetRepository:
    """Keep hard limits, reservations, and reconciliation in one authority."""

    def __init__(
        self,
        database_url: str,
        *,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        """Use one database and one collision-resistant identity source."""
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        self._database_url = database_url
        self._identity_factory = identity_factory

    def put_host_ceiling(
        self,
        context: RequestContext,
        *,
        service_id: str,
        workspace_id: str,
        amount: Decimal,
        currency: str,
        expected_revision: str | None,
        idempotency_key: str,
        reason: str,
        now: datetime,
    ) -> HostCeiling:
        """Create or replace the non-bypassable host workspace ceiling."""
        _require_ceiling_authority(context, service_id, workspace_id, write=True)
        amount = exact_decimal(amount)
        currency = currency_code(currency)
        _idempotency(idempotency_key)
        _bounded(reason, 500, "reason")
        _aware(now)
        fingerprint = _fingerprint(
            {
                "amount": str(amount),
                "currency": currency,
                "expected_revision": expected_revision,
                "reason": reason,
                "service_id": service_id,
                "workspace_id": workspace_id,
            }
        )
        with self._connection() as connection, connection.transaction():
            _lock(connection, "budget-hierarchy")
            _lock(connection, f"host-ceiling:{service_id}:{workspace_id}")
            replay = connection.execute(
                """SELECT operation_id::text, request_fingerprint,
                          resulting_revision::text, amount, currency::text,
                          effective_at
                   FROM router.budget_ceiling_operations
                   WHERE service_id = %s AND workspace_id = %s
                     AND actor_id = %s AND idempotency_key = %s""",
                (service_id, workspace_id, context.actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != fingerprint:
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                return HostCeiling(
                    service_id,
                    workspace_id,
                    Money(replay["amount"], replay["currency"]),
                    replay["resulting_revision"],
                    replay["effective_at"],
                    replay["operation_id"],
                )
            incompatible_limit = connection.execute(
                """SELECT 1 FROM router.budget_scopes AS budget
                   WHERE budget.service_id = %s
                     AND budget.scope_kind IN ('workspace', 'assignment')
                     AND (budget.workspace_id = %s
                          OR (budget.scope_kind = 'assignment'
                              AND budget.workspace_id IS NULL))
                     AND budget.currency <> %s
                   LIMIT 1""",
                (service_id, workspace_id, currency),
            ).fetchone()
            if incompatible_limit is not None:
                raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, context.request_id)
            current = connection.execute(
                """SELECT revision::text, budget_scope_id::text
                   FROM router.workspace_budget_ceilings
                   WHERE service_id = %s AND workspace_id = %s FOR UPDATE""",
                (service_id, workspace_id),
            ).fetchone()
            actual_revision = None if current is None else current["revision"]
            if actual_revision != expected_revision:
                raise BudgetError(BudgetErrorCode.CONFLICT, context.request_id)
            operation_id = self._identity_factory()
            revision = self._identity_factory()
            scope_id = (
                self._identity_factory()
                if current is None
                else current["budget_scope_id"]
            )
            if current is None:
                connection.execute(
                    """INSERT INTO router.budget_scopes (
                           id, scope_kind, service_id, workspace_id, currency,
                           hard_limit, revision, effective_at,
                           host_ceiling_revision
                       ) VALUES (
                           %s, 'host_ceiling', %s, %s, %s, %s, 1, %s, %s
                       )""",
                    (
                        scope_id,
                        service_id,
                        workspace_id,
                        currency,
                        amount,
                        now,
                        revision,
                    ),
                )
                connection.execute(
                    """INSERT INTO router.workspace_budget_ceilings (
                           service_id, workspace_id, budget_scope_id, amount,
                           currency, revision, operation_id, effective_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        service_id,
                        workspace_id,
                        scope_id,
                        amount,
                        currency,
                        revision,
                        operation_id,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE router.budget_scopes
                       SET currency = %s, hard_limit = %s,
                           revision = revision + 1, effective_at = %s,
                           host_ceiling_revision = %s
                       WHERE id = %s""",
                    (currency, amount, now, revision, scope_id),
                )
                connection.execute(
                    """UPDATE router.workspace_budget_ceilings
                       SET amount = %s, currency = %s, revision = %s,
                           operation_id = %s, effective_at = %s
                       WHERE service_id = %s AND workspace_id = %s""",
                    (
                        amount,
                        currency,
                        revision,
                        operation_id,
                        now,
                        service_id,
                        workspace_id,
                    ),
                )
            connection.execute(
                """INSERT INTO router.budget_ceiling_operations (
                       operation_id, service_id, workspace_id, actor_id,
                       idempotency_key, request_fingerprint, expected_revision,
                       resulting_revision, amount, currency, reason,
                       audit_event_id, effective_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    operation_id,
                    service_id,
                    workspace_id,
                    context.actor_id,
                    idempotency_key,
                    fingerprint,
                    expected_revision,
                    revision,
                    amount,
                    currency,
                    reason,
                    operation_id,
                    now,
                ),
            )
            _insert_audit(
                connection,
                context,
                event_id=operation_id,
                action="budget_ceiling.write",
                service_id=service_id,
                workspace_id=workspace_id,
                resource_type="workspace_budget_ceiling",
                now=now,
            )
        return HostCeiling(
            service_id,
            workspace_id,
            Money(amount, currency),
            str(revision),
            now,
            str(operation_id),
        )

    def get_host_ceiling(
        self, context: RequestContext, *, service_id: str, workspace_id: str
    ) -> HostCeiling:
        """Read the exact host-owned workspace ceiling."""
        _require_ceiling_authority(context, service_id, workspace_id, write=False)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT amount, currency::text, revision::text, effective_at,
                          operation_id::text
                   FROM router.workspace_budget_ceilings
                   WHERE service_id = %s AND workspace_id = %s""",
                (service_id, workspace_id),
            ).fetchone()
        if row is None:
            raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
        return HostCeiling(
            service_id,
            workspace_id,
            Money(row["amount"], row["currency"]),
            row["revision"],
            row["effective_at"],
            row["operation_id"],
        )

    def put_limit(
        self,
        context: RequestContext,
        target: BudgetTarget,
        *,
        hard_limit: Decimal,
        currency: str,
        warning_threshold: Decimal | None,
        reset_period: ResetPeriod,
        expected_revision: str,
        idempotency_key: str,
        now: datetime,
    ) -> BudgetLimit:
        """Create or replace one subordinate hard limit with durable history."""
        _require_limit_write(context, target)
        hard_limit = exact_decimal(hard_limit)
        warning = (
            None if warning_threshold is None else exact_decimal(warning_threshold)
        )
        currency = currency_code(currency)
        if warning is not None and warning > hard_limit:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        _idempotency(idempotency_key)
        _aware(now)
        try:
            expected_number = int(expected_revision)
        except ValueError as error:
            raise BudgetError(
                BudgetErrorCode.INVALID_REQUEST, context.request_id
            ) from error
        if expected_number < 0:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        fingerprint = _fingerprint(
            {
                "currency": currency,
                "expected_revision": expected_revision,
                "hard_limit": str(hard_limit),
                "reset_period": reset_period.value,
                "target": _target_value(target),
                "warning_threshold": None if warning is None else str(warning),
            }
        )
        with self._connection() as connection, connection.transaction():
            _lock(connection, "budget-hierarchy")
            if target.workspace_id is not None:
                _lock(
                    connection,
                    f"host-ceiling:{target.service_id}:{target.workspace_id}",
                )
            _lock(connection, f"budget-limit:{context.actor_id}:{idempotency_key}")
            replay = connection.execute(
                """SELECT operation.request_fingerprint,
                          operation.budget_scope_id::text, operation.hard_limit,
                          operation.warning_threshold, operation.currency::text,
                          operation.reset_period, operation.resulting_revision::text,
                          operation.effective_at, budget.scope_kind,
                          budget.service_id::text, budget.workspace_id::text,
                          budget.assignment_id::text
                   FROM router.budget_limit_operations AS operation
                   JOIN router.budget_scopes AS budget
                     ON budget.id = operation.budget_scope_id
                   WHERE operation.actor_id = %s AND operation.idempotency_key = %s""",
                (context.actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != fingerprint:
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                return _limit_from_operation(replay)
            _require_exact_limit_target(connection, target, context.request_id)
            if target.workspace_id is not None:
                ceiling = connection.execute(
                    """SELECT amount, currency::text
                       FROM router.workspace_budget_ceilings
                       WHERE service_id = %s AND workspace_id = %s""",
                    (target.service_id, target.workspace_id),
                ).fetchone()
                if ceiling is not None and ceiling["currency"] != currency:
                    raise BudgetError(
                        BudgetErrorCode.CURRENCY_MISMATCH, context.request_id
                    )
                if ceiling is not None and hard_limit > ceiling["amount"]:
                    raise BudgetError(
                        BudgetErrorCode.INVALID_REQUEST, context.request_id
                    )
            elif target.kind is BudgetScopeKind.ASSIGNMENT:
                ceilings = connection.execute(
                    """SELECT amount, currency::text
                       FROM router.workspace_budget_ceilings
                       WHERE service_id = %s ORDER BY workspace_id""",
                    (target.service_id,),
                ).fetchall()
                if any(row["currency"] != currency for row in ceilings):
                    raise BudgetError(
                        BudgetErrorCode.CURRENCY_MISMATCH, context.request_id
                    )
                if any(hard_limit > row["amount"] for row in ceilings):
                    raise BudgetError(
                        BudgetErrorCode.INVALID_REQUEST, context.request_id
                    )
            current = connection.execute(
                """SELECT id::text, revision, currency::text
                   FROM router.budget_scopes
                   WHERE scope_kind = %s
                     AND service_id IS NOT DISTINCT FROM %s
                     AND workspace_id IS NOT DISTINCT FROM %s
                     AND assignment_id IS NOT DISTINCT FROM %s
                   FOR UPDATE""",
                (
                    target.kind.value,
                    target.service_id,
                    target.workspace_id,
                    target.assignment_id,
                ),
            ).fetchone()
            actual = 0 if current is None else int(current["revision"])
            if actual != expected_number:
                raise BudgetError(BudgetErrorCode.CONFLICT, context.request_id)
            if current is not None and current["currency"] != currency:
                raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, context.request_id)
            _require_compatible_route_prices(
                connection, target, currency, context.request_id
            )
            scope_id = self._identity_factory() if current is None else current["id"]
            parent_id = _nearest_parent(connection, target, current_scope_id=scope_id)
            _validate_limit_hierarchy(
                connection,
                target,
                current_scope_id=scope_id,
                parent_id=parent_id,
                currency=currency,
                hard_limit=hard_limit,
                request_id=context.request_id,
            )
            operation_id = self._identity_factory()
            revision = actual + 1
            if current is None:
                connection.execute(
                    """INSERT INTO router.budget_scopes (
                           id, scope_kind, service_id, workspace_id, assignment_id,
                           parent_budget_scope_id, currency, hard_limit,
                           warning_threshold, revision, reset_period, effective_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        scope_id,
                        target.kind.value,
                        target.service_id,
                        target.workspace_id,
                        target.assignment_id,
                        parent_id,
                        currency,
                        hard_limit,
                        warning,
                        revision,
                        reset_period.value,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE router.budget_scopes
                       SET parent_budget_scope_id = %s, currency = %s,
                           hard_limit = %s, warning_threshold = %s,
                           revision = %s, reset_period = %s, effective_at = %s
                       WHERE id = %s""",
                    (
                        parent_id,
                        currency,
                        hard_limit,
                        warning,
                        revision,
                        reset_period.value,
                        now,
                        scope_id,
                    ),
                )
            connection.execute(
                """INSERT INTO router.budget_limit_operations (
                       operation_id, budget_scope_id, actor_id, idempotency_key,
                       request_fingerprint, expected_revision, resulting_revision,
                       hard_limit, warning_threshold, currency, reset_period,
                       audit_event_id, effective_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    operation_id,
                    scope_id,
                    context.actor_id,
                    idempotency_key,
                    fingerprint,
                    actual,
                    revision,
                    hard_limit,
                    warning,
                    currency,
                    reset_period.value,
                    operation_id,
                    now,
                ),
            )
            connection.execute(
                """UPDATE router.budget_scopes AS budget
                   SET parent_budget_scope_id = router.expected_budget_parent(
                       budget.scope_kind, budget.service_id,
                       budget.workspace_id, budget.id
                   )
                   WHERE budget.parent_budget_scope_id IS DISTINCT FROM
                       router.expected_budget_parent(
                           budget.scope_kind, budget.service_id,
                           budget.workspace_id, budget.id
                       )"""
            )
            _insert_audit(
                connection,
                context,
                event_id=operation_id,
                action="budget.write",
                service_id=target.service_id,
                workspace_id=target.workspace_id,
                resource_type="budget_limit",
                now=now,
            )
        return BudgetLimit(
            str(scope_id),
            target,
            Money(hard_limit, currency),
            None if warning is None else Money(warning, currency),
            reset_period,
            str(revision),
            now,
        )

    def reserve_candidate(
        self,
        context: RequestContext,
        *,
        request_row_id: str,
        candidate_id: str,
        reservation_key: str | None = None,
        candidate_kind: BudgetCandidateKind = BudgetCandidateKind.PROVIDER_ROUTE,
        estimated_amount: Decimal,
        reserved_amount: Decimal,
        currency: str,
        maximum_cost: Decimal | None = None,
        embedding: bool = False,
        more_candidates: bool = True,
        now: datetime,
    ) -> ReservationResult:
        """Atomically reserve one candidate against all applicable budgets."""
        _require_system(context, "budget.reserve")
        reservation_key = candidate_id if reservation_key is None else reservation_key
        _bounded(reservation_key, 200, "reservation key")
        estimate = exact_decimal(estimated_amount)
        reserved = exact_decimal(reserved_amount)
        if reserved < estimate:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        maximum = None if maximum_cost is None else exact_decimal(maximum_cost)
        currency = currency_code(currency)
        _aware(now)
        request_fingerprint = _fingerprint(
            {
                "candidate_id": candidate_id,
                "candidate_kind": candidate_kind.value,
                "currency": currency,
                "embedding": embedding,
                "estimated_amount": str(estimate),
                "maximum_cost": None if maximum is None else str(maximum),
                "more_candidates": more_candidates,
                "reservation_key": reservation_key,
                "reserved_amount": str(reserved),
            }
        )
        with self._connection() as connection, connection.transaction():
            _lock(connection, "budget-hierarchy")
            _lock(connection, f"request-budget:{request_row_id}")
            request = connection.execute(
                """SELECT service_id::text, workspace_id::text,
                          assignment_id::text
                   FROM router.logical_requests WHERE row_id = %s FOR SHARE""",
                (request_row_id,),
            ).fetchone()
            if request is None:
                raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
            if request["workspace_id"] is not None:
                _lock(
                    connection,
                    f"host-ceiling:{request['service_id']}:{request['workspace_id']}",
                )
            budget_set = _get_or_create_set(
                connection,
                request_row_id=request_row_id,
                currency=currency,
                maximum=maximum,
                identity_factory=self._identity_factory,
                request_id=context.request_id,
            )
            rejected = connection.execute(
                """SELECT candidate_id::text, request_fingerprint,
                          rejected_scope, exhausted
                   FROM router.budget_rejections
                   WHERE request_row_id = %s AND reservation_key = %s""",
                (request_row_id, reservation_key),
            ).fetchone()
            if rejected is not None:
                if (
                    rejected["candidate_id"] != candidate_id
                    or rejected["request_fingerprint"] != request_fingerprint
                ):
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                return _skip(
                    request_row_id,
                    candidate_id,
                    currency,
                    BudgetScopeKind(rejected["rejected_scope"]),
                    more_candidates=not rejected["exhausted"],
                )
            existing = connection.execute(
                """SELECT id::text, candidate_id::text, candidate_kind,
                          request_fingerprint, estimated_amount, reserved_amount
                   FROM router.budget_candidate_reservations
                   WHERE budget_set_id = %s AND reservation_key = %s""",
                (budget_set["id"], reservation_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["candidate_id"] != candidate_id
                    or existing["candidate_kind"] != candidate_kind.value
                    or existing["request_fingerprint"] != request_fingerprint
                    or existing["estimated_amount"] != estimate
                    or existing["reserved_amount"] != reserved
                ):
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                accounting_scope_id = _accounting_scope_id(
                    connection, existing["id"], candidate_kind
                )
                return ReservationResult(
                    ReservationState.RESERVED,
                    request_row_id,
                    candidate_id,
                    currency,
                    existing["id"],
                    replayed=True,
                    accounting_scope_id=accounting_scope_id,
                )
            scopes = _applicable_scopes(
                connection,
                service_id=request["service_id"],
                workspace_id=request["workspace_id"],
                assignment_id=request["assignment_id"],
            )
            ceiling = _ceiling(
                connection, request["service_id"], request["workspace_id"]
            )
            if embedding and not any(
                scope.kind in {BudgetScopeKind.WORKSPACE, BudgetScopeKind.HOST_CEILING}
                for scope in scopes
            ):
                raise BudgetError(
                    BudgetErrorCode.BUDGET_REQUIRED,
                    context.request_id,
                    scope_kind=BudgetScopeKind.WORKSPACE.value,
                    candidate_id=candidate_id,
                )
            if any(scope.currency != currency for scope in scopes):
                raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, context.request_id)
            if ceiling is not None and ceiling["currency"] != currency:
                raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, context.request_id)
            logical = _logical_balance(connection, budget_set["id"])
            if maximum is not None and logical.consumed + reserved > maximum:
                _record_rejection(
                    connection,
                    identity_factory=self._identity_factory,
                    request_row_id=request_row_id,
                    candidate_id=candidate_id,
                    reservation_key=reservation_key,
                    request_fingerprint=request_fingerprint,
                    scope=BudgetScopeKind.LOGICAL_REQUEST,
                    exhausted=not more_candidates,
                    now=now,
                )
                return _skip(
                    request_row_id,
                    candidate_id,
                    currency,
                    BudgetScopeKind.LOGICAL_REQUEST,
                    more_candidates=more_candidates,
                )
            balances: list[tuple[_ScopeRow, _Balance]] = []
            for scope in scopes:
                balance = _scope_balance(connection, scope, now)
                balances.append((scope, balance))
                if balance.consumed + reserved > scope.limit:
                    _record_rejection(
                        connection,
                        identity_factory=self._identity_factory,
                        request_row_id=request_row_id,
                        candidate_id=candidate_id,
                        reservation_key=reservation_key,
                        request_fingerprint=request_fingerprint,
                        scope=scope.kind,
                        exhausted=not more_candidates,
                        now=now,
                    )
                    return _skip(
                        request_row_id,
                        candidate_id,
                        currency,
                        scope.kind,
                        more_candidates=more_candidates,
                    )
            if ceiling is not None:
                host_balance = _workspace_balance(
                    connection, request["service_id"], request["workspace_id"]
                )
                if host_balance.consumed + reserved > ceiling["amount"]:
                    _record_rejection(
                        connection,
                        identity_factory=self._identity_factory,
                        request_row_id=request_row_id,
                        candidate_id=candidate_id,
                        reservation_key=reservation_key,
                        request_fingerprint=request_fingerprint,
                        scope=BudgetScopeKind.HOST_CEILING,
                        exhausted=not more_candidates,
                        now=now,
                    )
                    return _skip(
                        request_row_id,
                        candidate_id,
                        currency,
                        BudgetScopeKind.HOST_CEILING,
                        more_candidates=more_candidates,
                    )
            reservation_id = self._identity_factory()
            connection.execute(
                """INSERT INTO router.budget_candidate_reservations (
                       id, budget_set_id, reservation_key, candidate_id,
                       candidate_kind, request_fingerprint,
                       estimated_amount, reserved_amount, created_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    reservation_id,
                    budget_set["id"],
                    reservation_key,
                    candidate_id,
                    candidate_kind.value,
                    request_fingerprint,
                    estimate,
                    reserved,
                    now,
                ),
            )
            summaries: list[EnforcementSummary] = []
            for scope, balance in balances:
                connection.execute(
                    """INSERT INTO router.budget_reservation_allocations (
                           reservation_id, budget_scope_id, reserved_amount
                       ) VALUES (%s, %s, %s)""",
                    (reservation_id, scope.scope_id, reserved),
                )
                connection.execute(
                    """INSERT INTO router.budget_ledger_entries (
                           event_id, reservation_id, budget_scope_id, event_kind,
                           amount, occurred_at
                       ) VALUES (%s, %s, %s, 'reservation', %s, %s)""",
                    (
                        self._identity_factory(),
                        reservation_id,
                        scope.scope_id,
                        reserved,
                        now,
                    ),
                )
                summaries.append(_summary(scope, balance, add_reserved=reserved))
        return ReservationResult(
            ReservationState.RESERVED,
            request_row_id,
            candidate_id,
            currency,
            str(reservation_id),
            summaries=tuple(summaries),
            accounting_scope_id=_select_accounting_scope_id(scopes, candidate_kind),
        )

    def reconcile(
        self,
        context: RequestContext,
        reservation_id: str,
        *,
        accounting_event_id: str,
        actual_amount: Decimal,
        now: datetime,
    ) -> bool:
        """Append final use and return the complete conservative reservation."""
        _require_system(context, "budget.reconcile")
        actual = exact_decimal(actual_amount)
        _aware(now)
        with self._connection() as connection, connection.transaction():
            _lock(connection, f"budget-reservation:{reservation_id}")
            reservation = connection.execute(
                """SELECT candidate.id::text, candidate.budget_set_id::text
                   FROM router.budget_candidate_reservations AS candidate
                   WHERE candidate.id = %s""",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
            _lock_reservation_scopes(connection, reservation_id)
            existing = connection.execute(
                """SELECT accounting_event_id::text, actual_amount, occurred_at
                   FROM router.budget_reservation_reconciliations
                   WHERE reservation_id = %s""",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["accounting_event_id"] != accounting_event_id
                    or existing["actual_amount"] != actual
                    or existing["occurred_at"] != now
                ):
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                return True
            connection.execute(
                """INSERT INTO router.budget_reservation_reconciliations (
                       reservation_id, accounting_event_id, actual_amount, occurred_at
                   ) VALUES (%s, %s, %s, %s)""",
                (reservation_id, accounting_event_id, actual, now),
            )
            allocations = connection.execute(
                """SELECT budget_scope_id::text, reserved_amount
                   FROM router.budget_reservation_allocations
                   WHERE reservation_id = %s ORDER BY budget_scope_id FOR SHARE""",
                (reservation_id,),
            ).fetchall()
            for allocation in allocations:
                _lock(connection, f"budget-scope:{allocation['budget_scope_id']}")
                connection.execute(
                    """INSERT INTO router.budget_ledger_entries (
                           event_id, reservation_id, budget_scope_id, event_kind,
                           amount, occurred_at
                       ) VALUES (%s, %s, %s, 'usage', %s, %s),
                                (%s, %s, %s, 'release', %s, %s)""",
                    (
                        self._identity_factory(),
                        reservation_id,
                        allocation["budget_scope_id"],
                        actual,
                        now,
                        self._identity_factory(),
                        reservation_id,
                        allocation["budget_scope_id"],
                        allocation["reserved_amount"],
                        now,
                    ),
                )
        return False

    def append_correction(
        self,
        context: RequestContext,
        reservation_id: str,
        *,
        correction_id: str,
        accounting_correction_id: str,
        amount_delta: Decimal,
        reason: str,
        now: datetime,
    ) -> bool:
        """Append a late signed correction without changing completed work."""
        _require_system(context, "budget.correct")
        delta = exact_decimal(amount_delta, signed=True)
        _bounded(reason, 500, "reason")
        _aware(now)
        with self._connection() as connection, connection.transaction():
            _lock(connection, f"budget-reservation:{reservation_id}")
            _lock_reservation_scopes(connection, reservation_id)
            existing = connection.execute(
                """SELECT reservation_id::text, accounting_correction_id::text,
                          amount_delta, reason, occurred_at
                   FROM router.budget_reservation_corrections
                   WHERE correction_id = %s""",
                (correction_id,),
            ).fetchone()
            expected = (
                reservation_id,
                accounting_correction_id,
                delta,
                reason,
                now,
            )
            if existing is not None:
                if tuple(existing.values()) != expected:
                    raise BudgetError(
                        BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                    )
                return True
            reconciliation = connection.execute(
                """SELECT actual_amount FROM router.budget_reservation_reconciliations
                   WHERE reservation_id = %s FOR SHARE""",
                (reservation_id,),
            ).fetchone()
            if reconciliation is None:
                raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
            prior = connection.execute(
                """SELECT COALESCE(sum(amount_delta), 0) AS amount
                   FROM router.budget_reservation_corrections
                   WHERE reservation_id = %s""",
                (reservation_id,),
            ).fetchone()
            if prior is None:
                raise RuntimeError("The budget correction query returned no row.")
            if reconciliation["actual_amount"] + prior["amount"] + delta < 0:
                raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
            connection.execute(
                """INSERT INTO router.budget_reservation_corrections (
                       correction_id, reservation_id, accounting_correction_id,
                       amount_delta, reason, occurred_at
                   ) VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    correction_id,
                    reservation_id,
                    accounting_correction_id,
                    delta,
                    reason,
                    now,
                ),
            )
            allocations = connection.execute(
                """SELECT allocation.budget_scope_id::text,
                          usage.event_id::text AS usage_event_id
                   FROM router.budget_reservation_allocations AS allocation
                   JOIN router.budget_ledger_entries AS usage
                     ON usage.reservation_id = allocation.reservation_id
                    AND usage.budget_scope_id = allocation.budget_scope_id
                    AND usage.event_kind = 'usage'
                   WHERE allocation.reservation_id = %s
                   ORDER BY allocation.budget_scope_id""",
                (reservation_id,),
            ).fetchall()
            for allocation in allocations:
                connection.execute(
                    """INSERT INTO router.budget_ledger_entries (
                           event_id, reservation_id, budget_scope_id, event_kind,
                           amount, source_event_id, source_correction_id, occurred_at
                       ) VALUES (%s, %s, %s, 'correction', %s, %s, %s, %s)""",
                    (
                        self._identity_factory(),
                        reservation_id,
                        allocation["budget_scope_id"],
                        delta,
                        allocation["usage_event_id"],
                        correction_id,
                        now,
                    ),
                )
        return False

    def summary(
        self,
        context: RequestContext,
        target: BudgetTarget,
        *,
        now: datetime,
    ) -> EnforcementSummary:
        """Return a content-free exact summary for one authorized scope."""
        _require_limit_read(context, target)
        _aware(now)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT id::text, scope_kind, currency::text, hard_limit,
                          warning_threshold, reset_period, revision::text
                   FROM router.budget_scopes
                   WHERE scope_kind = %s
                     AND service_id IS NOT DISTINCT FROM %s
                     AND workspace_id IS NOT DISTINCT FROM %s
                     AND assignment_id IS NOT DISTINCT FROM %s""",
                (
                    target.kind.value,
                    target.service_id,
                    target.workspace_id,
                    target.assignment_id,
                ),
            ).fetchone()
            if row is None:
                raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
            scope = _scope_row(row)
            return _summary(scope, _scope_balance(connection, scope, now))

    def _connection(self) -> psycopg.Connection[DictRow]:
        return psycopg.connect(self._database_url, row_factory=dict_row)


def _applicable_scopes(
    connection: Connection[DictRow],
    *,
    service_id: str,
    workspace_id: str | None,
    assignment_id: str | None,
) -> tuple[_ScopeRow, ...]:
    rows = connection.execute(
        """WITH RECURSIVE ancestors AS (
               SELECT id, parent_service_id FROM router.services WHERE id = %s
             UNION ALL
               SELECT service.id, service.parent_service_id
               FROM router.services AS service
               JOIN ancestors ON ancestors.parent_service_id = service.id
           )
           SELECT budget.id::text, budget.scope_kind, budget.currency::text,
                  budget.hard_limit, budget.warning_threshold,
                  budget.reset_period, budget.revision::text
           FROM router.budget_scopes AS budget
           WHERE budget.scope_kind = 'global'
              OR (budget.scope_kind = 'service'
                  AND budget.service_id IN (SELECT id FROM ancestors))
              OR (budget.scope_kind = 'workspace' AND budget.service_id = %s
                  AND budget.workspace_id IS NOT DISTINCT FROM %s)
              OR (budget.scope_kind = 'host_ceiling' AND budget.service_id = %s
                  AND budget.workspace_id IS NOT DISTINCT FROM %s)
              OR (budget.scope_kind = 'assignment' AND budget.service_id = %s
                  AND budget.assignment_id IS NOT DISTINCT FROM %s
                  AND (budget.workspace_id IS NULL
                       OR budget.workspace_id IS NOT DISTINCT FROM %s))
           ORDER BY budget.id
           FOR UPDATE OF budget""",
        (
            service_id,
            service_id,
            workspace_id,
            service_id,
            workspace_id,
            service_id,
            assignment_id,
            workspace_id,
        ),
    ).fetchall()
    return tuple(_scope_row(row) for row in rows)


def _scope_row(row: DictRow) -> _ScopeRow:
    return _ScopeRow(
        row["id"],
        BudgetScopeKind(row["scope_kind"]),
        row["currency"],
        row["hard_limit"],
        row["warning_threshold"],
        ResetPeriod(row["reset_period"]),
        row["revision"],
    )


def _select_accounting_scope_id(
    scopes: tuple[_ScopeRow, ...], candidate_kind: BudgetCandidateKind
) -> str | None:
    """Select the most exact scope that the accounting subject can name."""
    priority = {
        BudgetScopeKind.HOST_CEILING: 0,
        BudgetScopeKind.ASSIGNMENT: 1,
        BudgetScopeKind.WORKSPACE: 2,
        BudgetScopeKind.SERVICE: 3,
        BudgetScopeKind.GLOBAL: 4,
    }
    eligible = (
        scopes
        if candidate_kind is BudgetCandidateKind.PROVIDER_ROUTE
        else tuple(
            scope for scope in scopes if scope.kind is not BudgetScopeKind.ASSIGNMENT
        )
    )
    if not eligible:
        return None
    selected = min(eligible, key=lambda scope: (priority[scope.kind], scope.scope_id))
    return selected.scope_id


def _accounting_scope_id(
    connection: Connection[DictRow],
    reservation_id: str,
    candidate_kind: BudgetCandidateKind,
) -> str | None:
    rows = connection.execute(
        """SELECT budget.id::text, budget.scope_kind, budget.currency::text,
                  budget.hard_limit, budget.warning_threshold,
                  budget.reset_period, budget.revision::text
           FROM router.budget_reservation_allocations AS allocation
           JOIN router.budget_scopes AS budget
             ON budget.id = allocation.budget_scope_id
           WHERE allocation.reservation_id = %s""",
        (reservation_id,),
    ).fetchall()
    return _select_accounting_scope_id(
        tuple(_scope_row(row) for row in rows), candidate_kind
    )


def _scope_balance(
    connection: Connection[DictRow], scope: _ScopeRow, now: datetime
) -> _Balance:
    start = _period_start(scope.reset_period, now)
    if start is None:
        query = """SELECT
               COALESCE(sum(CASE
                   WHEN event_kind = 'reservation' THEN amount
                   WHEN event_kind = 'release' THEN -amount ELSE 0 END), 0) AS reserved,
               COALESCE(sum(CASE WHEN event_kind = 'usage' THEN amount ELSE 0 END), 0) AS used,
               COALESCE(sum(CASE WHEN event_kind = 'correction' THEN amount ELSE 0 END), 0) AS corrected
           FROM router.budget_ledger_entries WHERE budget_scope_id = %s"""
        params: tuple[object, ...] = (scope.scope_id,)
    else:
        query = """SELECT
               COALESCE(sum(CASE
                   WHEN ledger.event_kind = 'reservation' THEN ledger.amount
                   WHEN ledger.event_kind = 'release' THEN -ledger.amount
                   ELSE 0 END), 0) AS reserved,
               COALESCE(sum(CASE WHEN ledger.event_kind = 'usage'
                   AND ledger.occurred_at >= %s
                   THEN ledger.amount ELSE 0 END), 0) AS used,
               COALESCE(sum(CASE WHEN ledger.event_kind = 'correction'
                   AND EXISTS (
                       SELECT 1 FROM router.budget_ledger_entries AS source
                       WHERE source.event_id = ledger.source_event_id
                         AND source.occurred_at >= %s
                   ) THEN ledger.amount ELSE 0 END), 0) AS corrected
           FROM router.budget_ledger_entries AS ledger
           WHERE ledger.budget_scope_id = %s"""
        params = (start, start, scope.scope_id)
    row = connection.execute(query, params).fetchone()
    if row is None:
        raise RuntimeError("The budget balance query returned no row.")
    return _Balance(row["reserved"], row["used"], row["corrected"])


def _logical_balance(connection: Connection[DictRow], budget_set_id: str) -> _Balance:
    row = connection.execute(
        """SELECT
               COALESCE(sum(CASE WHEN reconciliation.reservation_id IS NULL
                                 THEN candidate.reserved_amount ELSE 0 END), 0) AS reserved,
               COALESCE(sum(reconciliation.actual_amount), 0) AS used,
               COALESCE(sum(corrections.amount), 0) AS corrected
           FROM router.budget_candidate_reservations AS candidate
           LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
             ON reconciliation.reservation_id = candidate.id
           LEFT JOIN LATERAL (
               SELECT sum(amount_delta) AS amount
               FROM router.budget_reservation_corrections
               WHERE reservation_id = candidate.id
           ) AS corrections ON true
           WHERE candidate.budget_set_id = %s""",
        (budget_set_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("The logical budget query returned no row.")
    return _Balance(row["reserved"], row["used"], row["corrected"])


def _workspace_balance(
    connection: Connection[DictRow], service_id: str, workspace_id: str
) -> _Balance:
    row = connection.execute(
        """SELECT
               COALESCE(sum(CASE WHEN reconciliation.reservation_id IS NULL
                                 THEN candidate.reserved_amount ELSE 0 END), 0) AS reserved,
               COALESCE(sum(reconciliation.actual_amount), 0) AS used,
               COALESCE(sum(corrections.amount), 0) AS corrected
           FROM router.logical_request_budget_sets AS budget_set
           JOIN router.logical_requests AS request ON request.row_id = budget_set.request_row_id
           JOIN router.budget_candidate_reservations AS candidate
             ON candidate.budget_set_id = budget_set.id
           LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
             ON reconciliation.reservation_id = candidate.id
           LEFT JOIN LATERAL (
               SELECT sum(amount_delta) AS amount
               FROM router.budget_reservation_corrections
               WHERE reservation_id = candidate.id
           ) AS corrections ON true
           WHERE request.service_id = %s AND request.workspace_id = %s""",
        (service_id, workspace_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("The workspace budget query returned no row.")
    return _Balance(row["reserved"], row["used"], row["corrected"])


def _lock_reservation_scopes(
    connection: Connection[DictRow], reservation_id: str
) -> None:
    request = connection.execute(
        """SELECT request.service_id::text, request.workspace_id::text
           FROM router.budget_candidate_reservations AS reservation
           JOIN router.logical_request_budget_sets AS budget_set
             ON budget_set.id = reservation.budget_set_id
           JOIN router.logical_requests AS request
             ON request.row_id = budget_set.request_row_id
           WHERE reservation.id = %s FOR SHARE OF request""",
        (reservation_id,),
    ).fetchone()
    connection.execute(
        """SELECT budget.id::text AS budget_scope_id,
                  budget.currency
           FROM router.budget_reservation_allocations AS allocation
           JOIN router.budget_scopes AS budget
             ON budget.id = allocation.budget_scope_id
           WHERE allocation.reservation_id = %s
           ORDER BY budget.id
           FOR UPDATE OF budget""",
        (reservation_id,),
    ).fetchall()
    if request is not None and request["workspace_id"] is not None:
        connection.execute(
            """SELECT revision FROM router.workspace_budget_ceilings
               WHERE service_id = %s AND workspace_id = %s FOR UPDATE""",
            (request["service_id"], request["workspace_id"]),
        )


def _record_rejection(
    connection: Connection[DictRow],
    *,
    identity_factory: Callable[[], uuid.UUID],
    request_row_id: str,
    candidate_id: str,
    reservation_key: str,
    request_fingerprint: bytes,
    scope: BudgetScopeKind,
    exhausted: bool,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO router.budget_rejections (
               id, request_row_id, candidate_id, reservation_key,
               request_fingerprint, rejected_scope, exhausted, occurred_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            identity_factory(),
            request_row_id,
            candidate_id,
            reservation_key,
            request_fingerprint,
            scope.value,
            exhausted,
            now,
        ),
    )


def _get_or_create_set(
    connection: Connection[DictRow],
    *,
    request_row_id: str,
    currency: str,
    maximum: Decimal | None,
    identity_factory: Callable[[], uuid.UUID],
    request_id: str,
) -> DictRow:
    row = connection.execute(
        """SELECT id::text, currency::text, maximum_cost
           FROM router.logical_request_budget_sets
           WHERE request_row_id = %s""",
        (request_row_id,),
    ).fetchone()
    if row is not None:
        if row["currency"] != currency:
            raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, request_id)
        if row["maximum_cost"] != maximum:
            raise BudgetError(BudgetErrorCode.IDEMPOTENCY_CONFLICT, request_id)
        return row
    identity = identity_factory()
    return connection.execute(
        """INSERT INTO router.logical_request_budget_sets (
               id, request_row_id, currency, maximum_cost
           ) VALUES (%s, %s, %s, %s)
           RETURNING id::text, currency::text, maximum_cost""",
        (identity, request_row_id, currency, maximum),
    ).fetchone()  # type: ignore[return-value]


def _ceiling(
    connection: Connection[DictRow], service_id: str, workspace_id: str | None
) -> DictRow | None:
    if workspace_id is None:
        return None
    return connection.execute(
        """SELECT amount, currency::text, revision::text
           FROM router.workspace_budget_ceilings
           WHERE service_id = %s AND workspace_id = %s FOR UPDATE""",
        (service_id, workspace_id),
    ).fetchone()


def _summary(
    scope: _ScopeRow, balance: _Balance, *, add_reserved: Decimal = Decimal(0)
) -> EnforcementSummary:
    reserved = balance.reserved + add_reserved
    effective_used = max(balance.used + balance.corrected, Decimal(0))
    consumed = reserved + effective_used
    remaining = max(scope.limit - consumed, Decimal(0))
    if consumed >= scope.limit:
        state = EnforcementState.EXHAUSTED
    elif scope.warning is not None and consumed >= scope.warning:
        state = EnforcementState.WARNING
    else:
        state = EnforcementState.AVAILABLE
    return EnforcementSummary(
        scope.kind,
        Money(scope.limit, scope.currency),
        Money(reserved, scope.currency),
        Money(balance.used, scope.currency),
        SignedMoney(balance.corrected, scope.currency),
        Money(remaining, scope.currency),
        state,
        scope.revision,
        None if scope.warning is None else Money(scope.warning, scope.currency),
        scope.reset_period,
    )


def _skip(
    request_row_id: str,
    candidate_id: str,
    currency: str,
    scope: BudgetScopeKind,
    *,
    more_candidates: bool,
) -> ReservationResult:
    return ReservationResult(
        ReservationState.SKIPPED if more_candidates else ReservationState.EXHAUSTED,
        request_row_id,
        candidate_id,
        currency,
        rejected_scope=scope,
    )


def _period_start(period: ResetPeriod, now: datetime) -> datetime | None:
    value = now.astimezone(UTC)
    if period is ResetPeriod.NONE:
        return None
    if period is ResetPeriod.DAILY:
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _require_exact_limit_target(
    connection: Connection[DictRow], target: BudgetTarget, request_id: str
) -> None:
    """Lock and validate the exact non-retired scope for one budget write."""
    if target.kind is BudgetScopeKind.GLOBAL:
        return
    service = connection.execute(
        "SELECT state FROM router.services WHERE id = %s FOR SHARE",
        (target.service_id,),
    ).fetchone()
    if service is None or service["state"] == "retired":
        raise BudgetError(BudgetErrorCode.NOT_FOUND, request_id)
    if target.workspace_id is None:
        return
    workspace = connection.execute(
        """SELECT state FROM router.workspaces
           WHERE id = %s AND service_id = %s FOR SHARE""",
        (target.workspace_id, target.service_id),
    ).fetchone()
    if workspace is None or workspace["state"] == "retired":
        raise BudgetError(BudgetErrorCode.NOT_FOUND, request_id)


def _require_compatible_route_prices(
    connection: Connection[DictRow],
    target: BudgetTarget,
    currency: str,
    request_id: str,
) -> None:
    """Require each current route price affected by the budget to use its currency."""
    include_descendants = target.kind in {
        BudgetScopeKind.GLOBAL,
        BudgetScopeKind.SERVICE,
    }
    rows = connection.execute(
        """WITH RECURSIVE affected_services AS (
               SELECT service.id
               FROM router.services AS service
               WHERE service.state <> 'retired'
                 AND (%s::boolean OR service.id = %s)
             UNION
               SELECT child.id
               FROM router.services AS child
               JOIN affected_services AS parent
                 ON child.parent_service_id = parent.id
               WHERE %s::boolean AND child.state <> 'retired'
           )
           SELECT DISTINCT version.currency::text
           FROM affected_services AS service
           JOIN router.provider_model_routes AS route
             ON route.state = 'active' AND route.current_revision IS NOT NULL
           JOIN router.provider_instances AS instance
             ON instance.id = route.provider_instance_id
            AND instance.state = 'active'
           JOIN router.configuration_price_bindings AS binding
             ON binding.configuration_revision_id = route.current_revision
            AND binding.provider_model_route_id = route.id
           JOIN router.route_price_versions AS version
             ON version.id = binding.price_version_id
            AND version.provider_model_route_id = route.id
           WHERE router.provider_route_is_eligible(route.id, service.id)
             AND router.provider_resource_is_enabled(
                 'provider_model_route', route.id, service.id, %s
             )
             AND router.provider_resource_is_enabled(
                 'provider_instance', instance.id, service.id, %s
             )
             AND (
                 %s::uuid IS NULL
                 OR EXISTS (
                     SELECT 1
                     FROM router.assignment_candidates AS candidate
                     JOIN router.active_configurations AS active
                       ON active.revision_id = candidate.configuration_revision_id
                     WHERE candidate.assignment_id = %s
                       AND candidate.provider_model_route_id = route.id
                 )
             )""",
        (
            target.kind is BudgetScopeKind.GLOBAL,
            target.service_id,
            include_descendants,
            target.workspace_id,
            target.workspace_id,
            target.assignment_id,
            target.assignment_id,
        ),
    ).fetchall()
    if any(row["currency"] != currency for row in rows):
        raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, request_id)


def _nearest_parent(
    connection: Connection[DictRow],
    target: BudgetTarget,
    *,
    current_scope_id: str | uuid.UUID,
) -> str | None:
    if target.kind is BudgetScopeKind.GLOBAL:
        return None
    rows = connection.execute(
        """WITH RECURSIVE ancestors AS (
               SELECT id, parent_service_id, 0 AS depth
               FROM router.services WHERE id = %s
             UNION ALL
               SELECT service.id, service.parent_service_id, ancestors.depth + 1
               FROM router.services AS service
               JOIN ancestors ON ancestors.parent_service_id = service.id
           )
           SELECT budget.id::text
           FROM router.budget_scopes AS budget
           LEFT JOIN ancestors ON budget.service_id = ancestors.id
           WHERE (
               budget.scope_kind = 'global'
               OR (budget.scope_kind = 'service' AND ancestors.id IS NOT NULL)
               OR (budget.scope_kind = 'workspace' AND budget.service_id = %s
                   AND budget.workspace_id IS NOT DISTINCT FROM %s)
           ) AND budget.id <> %s
           ORDER BY CASE budget.scope_kind
               WHEN 'workspace' THEN 0 WHEN 'service' THEN 1 ELSE 2 END,
               ancestors.depth NULLS LAST
           LIMIT 1""",
        (
            target.service_id,
            target.service_id,
            target.workspace_id,
            current_scope_id,
        ),
    ).fetchone()
    return None if rows is None else rows["id"]


def _validate_limit_hierarchy(
    connection: Connection[DictRow],
    target: BudgetTarget,
    *,
    current_scope_id: str | uuid.UUID,
    parent_id: str | None,
    currency: str,
    hard_limit: Decimal,
    request_id: str,
) -> None:
    if target.kind is BudgetScopeKind.WORKSPACE:
        incompatible = connection.execute(
            """SELECT 1 FROM router.budget_scopes
               WHERE scope_kind = 'assignment' AND service_id = %s
                 AND workspace_id IS NULL AND currency <> %s
               LIMIT 1""",
            (target.service_id, currency),
        ).fetchone()
    elif target.kind is BudgetScopeKind.ASSIGNMENT and target.workspace_id is None:
        incompatible = connection.execute(
            """SELECT 1 FROM router.budget_scopes
               WHERE scope_kind = 'workspace' AND service_id = %s
                 AND currency <> %s
               LIMIT 1""",
            (target.service_id, currency),
        ).fetchone()
    else:
        incompatible = None
    if incompatible is not None:
        raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, request_id)
    if parent_id is not None:
        parent = connection.execute(
            """SELECT currency::text, hard_limit
               FROM router.budget_scopes WHERE id = %s""",
            (parent_id,),
        ).fetchone()
        if parent is not None and parent["currency"] != currency:
            raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, request_id)
        if parent is not None and parent["hard_limit"] < hard_limit:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, request_id)
    descendants = connection.execute(
        """WITH RECURSIVE service_descendants AS (
               SELECT id FROM router.services WHERE id = %s
             UNION ALL
               SELECT service.id FROM router.services AS service
               JOIN service_descendants AS parent
                 ON service.parent_service_id = parent.id
           )
           SELECT budget.currency::text, budget.hard_limit
           FROM router.budget_scopes AS budget
           WHERE budget.id <> %s AND budget.scope_kind <> 'host_ceiling'
             AND (
                 %s = 'global'
                 OR (%s = 'service' AND budget.service_id IN (
                     SELECT id FROM service_descendants
                 ))
                 OR (%s = 'workspace'
                     AND budget.scope_kind = 'assignment'
                     AND budget.service_id = %s
                     AND budget.workspace_id IS NOT DISTINCT FROM %s)
             )""",
        (
            target.service_id,
            current_scope_id,
            target.kind.value,
            target.kind.value,
            target.kind.value,
            target.service_id,
            target.workspace_id,
        ),
    ).fetchall()
    if any(row["currency"] != currency for row in descendants):
        raise BudgetError(BudgetErrorCode.CURRENCY_MISMATCH, request_id)
    if any(row["hard_limit"] > hard_limit for row in descendants):
        raise BudgetError(BudgetErrorCode.INVALID_REQUEST, request_id)


def _limit_from_operation(row: DictRow) -> BudgetLimit:
    target = BudgetTarget(
        BudgetScopeKind(row["scope_kind"]),
        row["service_id"],
        row["workspace_id"],
        row["assignment_id"],
    )
    return BudgetLimit(
        row["budget_scope_id"],
        target,
        Money(row["hard_limit"], row["currency"]),
        None
        if row["warning_threshold"] is None
        else Money(row["warning_threshold"], row["currency"]),
        ResetPeriod(row["reset_period"]),
        row["resulting_revision"],
        row["effective_at"],
    )


def _target_value(target: BudgetTarget) -> dict[str, str | None]:
    return {
        "kind": target.kind.value,
        "service_id": target.service_id,
        "workspace_id": target.workspace_id,
        "assignment_id": target.assignment_id,
    }


def _require_ceiling_authority(
    context: RequestContext, service_id: str, workspace_id: str, *, write: bool
) -> None:
    expected = "budget_ceiling.write" if write else "budget_ceiling.read"
    if not (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.BUDGET_AUTHORITY
        and context.operation == expected
        and context.scope == Scope(service_id, workspace_id)
        and context.actor_id == service_id
        and context.mutation is write
    ):
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_limit_write(context: RequestContext, target: BudgetTarget) -> None:
    scope = Scope(target.service_id, target.workspace_id)
    machine = (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.CONFIGURATION
        and context.actor_id == target.service_id
    )
    administrator = (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.machine_audience is None
        and (
            context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            or (
                target.kind is not BudgetScopeKind.GLOBAL
                and context.authority_class is AuthorityClass.SERVICE
            )
        )
    )
    embed = (
        context.actor_kind is PrincipalKind.EMBED
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.EMBED
        and context.machine_audience is None
    )
    if not (
        (machine or administrator or embed)
        and context.operation == "budget.write"
        and context.scope == scope
        and context.mutation
    ):
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_limit_read(context: RequestContext, target: BudgetTarget) -> None:
    scope = Scope(target.service_id, target.workspace_id)
    machine = (
        context.actor_kind is PrincipalKind.SERVICE
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is Audience.CONFIGURATION
        and context.actor_id == target.service_id
    )
    administrator = (
        context.actor_kind is PrincipalKind.ADMINISTRATOR
        and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        and context.machine_audience is None
        and (
            context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            or (
                target.kind is not BudgetScopeKind.GLOBAL
                and context.authority_class is AuthorityClass.SERVICE
            )
        )
    )
    embed = (
        context.actor_kind is PrincipalKind.EMBED
        and context.authority_class is AuthorityClass.SERVICE
        and context.authority_path is AuthorityPath.EMBED
        and context.machine_audience is None
        and context.actor_id != ""
    )
    if not (
        context.operation == "budget.read"
        and context.scope == scope
        and not context.mutation
        and (machine or administrator or embed)
    ):
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_system(context: RequestContext, operation: str) -> None:
    if not (
        context.actor_kind is PrincipalKind.SYSTEM
        and context.authority_class is AuthorityClass.SYSTEM
        and context.authority_path is AuthorityPath.MACHINE
        and context.machine_audience is None
        and context.operation == operation
        and context.scope == Scope()
        and context.mutation
    ):
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _insert_audit(
    connection: Connection[DictRow],
    context: RequestContext,
    *,
    event_id: uuid.UUID,
    action: str,
    service_id: str | None,
    workspace_id: str | None,
    resource_type: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO router.audit_events (
               event_id, audit_class, actor_kind, actor_id, authority_class,
               service_id, workspace_id, action, permission_result,
               safe_details, occurred_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'permitted', %s, %s)""",
        (
            event_id,
            "global_administration"
            if context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            else "security",
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            service_id,
            workspace_id,
            action,
            Jsonb({"resource_type": resource_type}),
            now,
        ),
    )


def _fingerprint(value: object) -> bytes:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _lock(connection: Connection[DictRow], key: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def _idempotency(value: str) -> None:
    if not 16 <= len(value) <= 200:
        raise ValueError("The idempotency key must contain from 16 to 200 characters.")


def _bounded(value: str, maximum: int, label: str) -> None:
    if not value or len(value) > maximum:
        msg = f"The {label} is empty or too long."
        raise ValueError(msg)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Budget times must include a time zone.")

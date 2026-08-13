"""Fenced central allowances and durable node-local consumption."""
# ruff: noqa: D105, D107, E501, EM101, PLR0913, PLR0917, PLR2004, TRY003

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import dict_row

from llmrouter_backend.accounting.model import currency_code, exact_decimal
from llmrouter_backend.authority import PrincipalKind, RequestContext
from llmrouter_backend.budgets.errors import BudgetError, BudgetErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from psycopg import Connection
    from psycopg.rows import DictRow


@dataclass(frozen=True, slots=True)
class AllowanceRequest:
    """One requested grant for one applicable hard-budget scope."""

    budget_scope_id: str
    amount: Decimal
    maximum_correction_risk: Decimal

    def __post_init__(self) -> None:
        if not self.budget_scope_id:
            raise ValueError("The budget scope identity must not be empty.")
        amount = exact_decimal(self.amount)
        risk = exact_decimal(self.maximum_correction_risk)
        if amount <= 0 or risk < 0:
            raise ValueError("Allowance amounts must be positive with nonnegative risk.")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "maximum_correction_risk", risk)


@dataclass(frozen=True, slots=True)
class AllowanceLease:
    """One safe node allowance state without sensitive accounting data."""

    lease_id: str
    batch_id: str
    budget_scope_id: str
    owner_node_id: str
    lease_generation: int
    currency: str
    issued_amount: Decimal
    maximum_correction_risk: Decimal
    issued_at: datetime
    expires_at: datetime
    safety_until: datetime

    def __post_init__(self) -> None:
        if not all((self.lease_id, self.batch_id, self.budget_scope_id, self.owner_node_id)):
            raise ValueError("Allowance identities must not be empty.")
        if self.lease_generation < 1:
            raise ValueError("The allowance generation must be positive.")
        object.__setattr__(self, "currency", currency_code(self.currency))
        object.__setattr__(self, "issued_amount", exact_decimal(self.issued_amount))
        object.__setattr__(
            self,
            "maximum_correction_risk",
            exact_decimal(self.maximum_correction_risk),
        )
        if self.issued_amount <= 0 or self.maximum_correction_risk < 0:
            raise ValueError("Allowance values must be positive with nonnegative risk.")
        _aware(self.issued_at)
        _aware(self.expires_at)
        _aware(self.safety_until)
        if not self.issued_at < self.expires_at <= self.safety_until:
            raise ValueError("Allowance times are not ordered.")


@dataclass(frozen=True, slots=True)
class AllowanceBatch:
    """One complete applicable set of node allowances."""

    batch_id: str
    lineage_id: str
    owner_node_id: str
    lease_generation: int
    service_id: str | None
    workspace_id: str | None
    assignment_id: str | None
    currency: str
    applicable_scope_ids: tuple[str, ...]
    leases: tuple[AllowanceLease, ...]

    def __post_init__(self) -> None:
        if (
            not self.batch_id
            or not self.lineage_id
            or not self.owner_node_id
            or self.lease_generation < 1
        ):
            raise ValueError("Allowance batch identities are invalid.")
        currency = currency_code(self.currency)
        if self.workspace_id is not None and self.service_id is None:
            raise ValueError("A workspace allowance needs its service identity.")
        if not self.leases:
            raise ValueError("An allowance batch must contain a complete scope set.")
        scope_ids = {lease.budget_scope_id for lease in self.leases}
        if scope_ids != set(self.applicable_scope_ids) or len(scope_ids) != len(
            self.applicable_scope_ids
        ) or any(
            lease.batch_id != self.batch_id
            or lease.owner_node_id != self.owner_node_id
            or lease.lease_generation != self.lease_generation
            or lease.currency != currency
            or lease.issued_at != self.leases[0].issued_at
            or lease.expires_at != self.leases[0].expires_at
            or lease.safety_until != self.leases[0].safety_until
            for lease in self.leases
        ):
            raise ValueError("Allowance leases do not match their complete batch.")
        object.__setattr__(self, "currency", currency)

    @property
    def maximum_correction_risk(self) -> Decimal:
        """Return the maximum visible late-correction exposure in the batch."""
        return max(lease.maximum_correction_risk for lease in self.leases)


@dataclass(frozen=True, slots=True)
class AllowanceDebit:
    """One durable local multi-scope debit result."""

    batch_id: str
    lease_generation: int
    amount: Decimal
    consumed_by_lease: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True, slots=True)
class AllowanceScopeState:
    """One safe local allowance balance and correction-risk bound."""

    budget_scope_id: str
    remaining_amount: Decimal
    maximum_correction_risk: Decimal


@dataclass(frozen=True, slots=True)
class AllowanceState:
    """One safe complete local allowance state."""

    batch_id: str
    lease_generation: int
    expires_at: datetime
    current: bool
    finalized: bool
    scopes: tuple[AllowanceScopeState, ...]


@dataclass(frozen=True, slots=True)
class AllowanceFinal:
    """One cumulative lease result for central reconciliation."""

    lease_id: str
    owner_node_id: str
    lease_generation: int
    used_amount: Decimal
    returned_amount: Decimal


class PostgresAllowanceRepository:
    """Issue and finalize bounded allowances in the central budget authority."""

    def __init__(
        self,
        database_url: str,
        *,
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if not database_url:
            raise ValueError("The database URL must not be empty.")
        self._database_url = database_url
        self._identity_factory = identity_factory

    def issue(
        self,
        context: RequestContext,
        *,
        owner_node_id: str,
        lease_generation: int,
        service_id: str | None,
        workspace_id: str | None,
        assignment_id: str | None,
        currency: str,
        requests: Iterable[AllowanceRequest],
        issued_at: datetime,
        expires_at: datetime,
        safety_until: datetime,
        lineage_id: str | None = None,
    ) -> AllowanceBatch:
        """Issue one complete applicable allowance set in one transaction."""
        _require_system(context, "budget.allowance.issue")
        _scope_shape(service_id, workspace_id)
        if not owner_node_id or lease_generation < 1:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        currency = currency_code(currency)
        _ordered_times(issued_at, expires_at, safety_until)
        requested = tuple(requests)
        if not requested or len({item.budget_scope_id for item in requested}) != len(
            requested
        ):
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        batch_id = self._identity_factory()
        lineage = self._identity_factory() if lineage_id is None else uuid.UUID(lineage_id)
        if lineage_id is None and lease_generation != 1:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        leases: list[AllowanceLease] = []
        try:
            with self._connection() as connection, connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0))"
                )
                rows = connection.execute(
                    _APPLICABLE_SCOPE_SQL,
                    (service_id, service_id, workspace_id, service_id, assignment_id, workspace_id),
                ).fetchall()
                applicable = {row["id"]: row["currency"] for row in rows}
                supplied = {item.budget_scope_id for item in requested}
                if supplied != set(applicable) or any(
                    applicable[item.budget_scope_id] != currency for item in requested
                ):
                    raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
                connection.execute(
                    """INSERT INTO router.budget_allowance_batches (
                           id, lineage_id, owner_node_id, lease_generation, service_id,
                           workspace_id, assignment_id, currency, issued_at,
                           expires_at, safety_until
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        batch_id,
                        lineage,
                        owner_node_id,
                        lease_generation,
                        service_id,
                        workspace_id,
                        assignment_id,
                        currency,
                        issued_at,
                        expires_at,
                        safety_until,
                    ),
                )
                for item in sorted(requested, key=lambda value: value.budget_scope_id):
                    lease_id = self._identity_factory()
                    connection.execute(
                        """INSERT INTO router.budget_allowance_leases (
                               id, batch_id, budget_scope_id, currency,
                               owner_node_id, lease_generation, issued_amount,
                               maximum_correction_risk, issued_at, expires_at,
                               safety_until
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            lease_id,
                            batch_id,
                            item.budget_scope_id,
                            currency,
                            owner_node_id,
                            lease_generation,
                            item.amount,
                            item.maximum_correction_risk,
                            issued_at,
                            expires_at,
                            safety_until,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO router.budget_allowance_ledger_entries (
                               event_id, allowance_lease_id, budget_scope_id,
                               event_kind, amount, occurred_at
                           ) VALUES (%s, %s, %s, 'grant', %s, %s)""",
                        (
                            self._identity_factory(),
                            lease_id,
                            item.budget_scope_id,
                            item.amount,
                            issued_at,
                        ),
                    )
                    leases.append(
                        AllowanceLease(
                            str(lease_id),
                            str(batch_id),
                            item.budget_scope_id,
                            owner_node_id,
                            lease_generation,
                            currency,
                            item.amount,
                            item.maximum_correction_risk,
                            issued_at,
                            expires_at,
                            safety_until,
                        )
                    )
        except psycopg.errors.CheckViolation as error:
            raise BudgetError(BudgetErrorCode.BUDGET_EXHAUSTED, context.request_id) from error
        except psycopg.errors.SerializationFailure as error:
            raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, context.request_id) from error
        return AllowanceBatch(
            str(batch_id),
            str(lineage),
            owner_node_id,
            lease_generation,
            service_id,
            workspace_id,
            assignment_id,
            currency,
            tuple(sorted(applicable)),
            tuple(leases),
        )

    def reconcile(
        self,
        context: RequestContext,
        final: AllowanceFinal,
        *,
        now: datetime,
    ) -> None:
        """Finalize one exact owner generation with used and returned amounts."""
        _require_node(context, "budget.allowance.reconcile", final.owner_node_id)
        _aware(now)
        used = exact_decimal(final.used_amount)
        returned = exact_decimal(final.returned_amount)
        reconciliation_id = self._identity_factory()
        try:
            with self._connection() as connection, connection.transaction():
                row = connection.execute(
                    """SELECT budget_scope_id::text, issued_at
                       FROM router.budget_allowance_leases
                       WHERE id = %s""",
                    (final.lease_id,),
                ).fetchone()
                if row is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                connection.execute(
                    """INSERT INTO router.budget_allowance_reconciliations (
                           reconciliation_id, allowance_lease_id, owner_node_id,
                           lease_generation, used_amount, returned_amount,
                           occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, false)""",
                    (
                        reconciliation_id,
                        final.lease_id,
                        final.owner_node_id,
                        final.lease_generation,
                        used,
                        returned,
                        now,
                    ),
                )
                self._insert_final_ledger(
                    connection,
                    final.lease_id,
                    row["budget_scope_id"],
                    reconciliation_id,
                    used,
                    returned,
                    row["issued_at"],
                )
        except psycopg.errors.UniqueViolation as error:
            raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, context.request_id) from error
        except (psycopg.errors.SerializationFailure, psycopg.errors.CheckViolation) as error:
            raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, context.request_id) from error

    def reclaim(
        self,
        context: RequestContext,
        *,
        lease_id: str,
        now: datetime,
    ) -> None:
        """Reclaim one unreported lease only after its safety window."""
        _require_system(context, "budget.allowance.reclaim")
        _aware(now)
        reconciliation_id = self._identity_factory()
        try:
            with self._connection() as connection, connection.transaction():
                row = connection.execute(
                    """SELECT budget_scope_id::text, owner_node_id::text,
                              lease_generation, issued_amount, issued_at
                       FROM router.budget_allowance_leases WHERE id = %s""",
                    (lease_id,),
                ).fetchone()
                if row is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                connection.execute(
                    """INSERT INTO router.budget_allowance_reconciliations (
                           reconciliation_id, allowance_lease_id, owner_node_id,
                           lease_generation, used_amount, returned_amount,
                           occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, 0, %s, true)""",
                    (
                        reconciliation_id,
                        lease_id,
                        row["owner_node_id"],
                        row["lease_generation"],
                        row["issued_amount"],
                        now,
                    ),
                )
                self._insert_final_ledger(
                    connection,
                    lease_id,
                    row["budget_scope_id"],
                    reconciliation_id,
                    row["issued_amount"],
                    Decimal(0),
                    row["issued_at"],
                )
        except (psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation) as error:
            raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, context.request_id) from error

    def append_correction(
        self,
        context: RequestContext,
        *,
        lease_id: str,
        amount_delta: Decimal,
        reason: str,
        now: datetime,
    ) -> None:
        """Append one bounded late usage correction after final reconciliation."""
        _require_system(context, "budget.allowance.correct")
        delta = exact_decimal(amount_delta)
        _aware(now)
        if not 1 <= len(reason) <= 500:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        correction_id = self._identity_factory()
        try:
            with self._connection() as connection, connection.transaction():
                row = connection.execute(
                    """SELECT budget_scope_id::text, issued_at
                       FROM router.budget_allowance_leases
                       WHERE id = %s""",
                    (lease_id,),
                ).fetchone()
                if row is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                connection.execute(
                    """INSERT INTO router.budget_allowance_corrections (
                           correction_id, allowance_lease_id, amount_delta,
                           reason, occurred_at
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (correction_id, lease_id, delta, reason, now),
                )
                connection.execute(
                    """INSERT INTO router.budget_allowance_ledger_entries (
                           event_id, allowance_lease_id, budget_scope_id,
                           event_kind, amount, source_correction_id, occurred_at
                       ) VALUES (%s, %s, %s, 'correction', %s, %s, %s)""",
                    (
                        self._identity_factory(),
                        lease_id,
                        row["budget_scope_id"],
                        delta,
                        correction_id,
                        row["issued_at"],
                    ),
                )
        except psycopg.errors.CheckViolation as error:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id) from error

    def _insert_final_ledger(
        self,
        connection: Connection[DictRow],
        lease_id: str,
        scope_id: str,
        reconciliation_id: uuid.UUID,
        used: Decimal,
        returned: Decimal,
        now: datetime,
    ) -> None:
        for kind, amount in (("usage", used), ("return", returned)):
            connection.execute(
                """INSERT INTO router.budget_allowance_ledger_entries (
                       event_id, allowance_lease_id, budget_scope_id,
                       event_kind, amount, source_reconciliation_id, occurred_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    self._identity_factory(),
                    lease_id,
                    scope_id,
                    kind,
                    amount,
                    reconciliation_id,
                    now,
                ),
            )

    def _connection(self) -> Connection[DictRow]:
        return psycopg.connect(self._database_url, row_factory=dict_row)


class SqliteAllowanceWallet:
    """Persist and atomically consume complete local allowance batches."""

    def __init__(self, path: str | Path, *, owner_node_id: str) -> None:
        if not owner_node_id:
            raise ValueError("The node identity must not be empty.")
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("An allowance wallet must use durable storage.")
        self._owner_node_id = owner_node_id
        self._lock = threading.Lock()
        with self._connection() as connection:
            connection.executescript(_WALLET_SCHEMA)

    def install(self, batch: AllowanceBatch) -> None:
        """Persist a trusted complete batch without restoring spent value."""
        if batch.owner_node_id != self._owner_node_id or not batch.leases:
            raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch.batch_id)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT lineage_id, owner_node_id, lease_generation,
                          service_id, workspace_id, assignment_id, currency,
                          issued_at, expires_at, safety_until
                   FROM allowance_batches WHERE batch_id = ?""",
                (batch.batch_id,),
            ).fetchone()
            if existing is not None:
                stored_leases = connection.execute(
                    """SELECT lease_id, budget_scope_id, issued_amount,
                              maximum_correction_risk
                       FROM allowance_leases WHERE batch_id = ? ORDER BY lease_id""",
                    (batch.batch_id,),
                ).fetchall()
                expected_batch = (
                    batch.lineage_id,
                    batch.owner_node_id,
                    batch.lease_generation,
                    batch.service_id,
                    batch.workspace_id,
                    batch.assignment_id,
                    batch.currency,
                    batch.leases[0].issued_at.isoformat(),
                    batch.leases[0].expires_at.isoformat(),
                    batch.leases[0].safety_until.isoformat(),
                )
                expected_leases = sorted(
                    (
                        lease.lease_id,
                        lease.budget_scope_id,
                        str(lease.issued_amount),
                        str(lease.maximum_correction_risk),
                    )
                    for lease in batch.leases
                )
                if existing != expected_batch or stored_leases != expected_leases:
                    raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch.batch_id)
                connection.commit()
                return
            connection.execute(
                """INSERT INTO allowance_batches (
                       batch_id, lineage_id, owner_node_id, lease_generation, service_id,
                       workspace_id, assignment_id, currency, issued_at,
                       expires_at, safety_until
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch.batch_id,
                    batch.lineage_id,
                    batch.owner_node_id,
                    batch.lease_generation,
                    batch.service_id,
                    batch.workspace_id,
                    batch.assignment_id,
                    batch.currency,
                    batch.leases[0].issued_at.isoformat(),
                    batch.leases[0].expires_at.isoformat(),
                    batch.leases[0].safety_until.isoformat(),
                ),
            )
            for lease in batch.leases:
                if lease.batch_id != batch.batch_id or lease.owner_node_id != batch.owner_node_id:
                    raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch.batch_id)
                connection.execute(
                    """INSERT INTO allowance_leases (
                           lease_id, batch_id, budget_scope_id, issued_amount,
                           maximum_correction_risk, consumed_amount
                       ) VALUES (?, ?, ?, ?, ?, '0')""",
                    (
                        lease.lease_id,
                        batch.batch_id,
                        lease.budget_scope_id,
                        str(lease.issued_amount),
                        str(lease.maximum_correction_risk),
                    ),
                )
            connection.commit()

    def consume(
        self,
        batch_id: str,
        amount: Decimal,
        *,
        service_id: str | None,
        workspace_id: str | None,
        assignment_id: str | None,
        now: datetime,
    ) -> AllowanceDebit:
        """Debit every scope in the current batch in one durable transaction."""
        amount = exact_decimal(amount)
        _aware(now)
        if amount <= 0:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, batch_id)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                """SELECT owner_node_id, lease_generation, service_id,
                          workspace_id, assignment_id, expires_at, finalized
                   FROM allowance_batches WHERE batch_id = ?""",
                (batch_id,),
            ).fetchone()
            if batch is None or batch[0] != self._owner_node_id:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if batch[6]:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if (batch[2], batch[3], batch[4]) != (
                service_id,
                workspace_id,
                assignment_id,
            ):
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            highest = connection.execute(
                """SELECT max(lease_generation) FROM allowance_batches
                   WHERE owner_node_id = ? AND lineage_id = (
                       SELECT lineage_id FROM allowance_batches WHERE batch_id = ?
                   )""",
                (self._owner_node_id, batch_id),
            ).fetchone()[0]
            if batch[1] != highest:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if now >= datetime.fromisoformat(batch[5]):
                raise BudgetError(BudgetErrorCode.ALLOWANCE_EXPIRED, batch_id)
            rows = connection.execute(
                """SELECT lease_id, issued_amount, consumed_amount
                   FROM allowance_leases WHERE batch_id = ? ORDER BY lease_id""",
                (batch_id,),
            ).fetchall()
            if not rows or any(
                Decimal(row[1]) - Decimal(row[2]) < amount for row in rows
            ):
                raise BudgetError(BudgetErrorCode.ALLOWANCE_EXHAUSTED, batch_id)
            consumed: list[tuple[str, Decimal]] = []
            for lease_id, _issued, prior in rows:
                cumulative = Decimal(prior) + amount
                connection.execute(
                    "UPDATE allowance_leases SET consumed_amount = ? WHERE lease_id = ?",
                    (str(cumulative), lease_id),
                )
                consumed.append((lease_id, cumulative))
            connection.commit()
            return AllowanceDebit(batch_id, batch[1], amount, tuple(consumed))

    def final(self, batch_id: str) -> tuple[AllowanceFinal, ...]:
        """Close a complete batch and return its durable cumulative values."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT owner_node_id, lease_generation FROM allowance_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None or batch[0] != self._owner_node_id:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            rows = connection.execute(
                """SELECT lease_id, issued_amount, consumed_amount
                   FROM allowance_leases WHERE batch_id = ? ORDER BY lease_id""",
                (batch_id,),
            ).fetchall()
            connection.execute(
                "UPDATE allowance_batches SET finalized = 1 WHERE batch_id = ?",
                (batch_id,),
            )
            result = tuple(
                AllowanceFinal(
                    row[0],
                    self._owner_node_id,
                    batch[1],
                    Decimal(row[2]),
                    Decimal(row[1]) - Decimal(row[2]),
                )
                for row in rows
            )
            connection.commit()
            return result

    def state(self, batch_id: str, *, now: datetime) -> AllowanceState:
        """Return safe local balance, expiry, fencing, and correction-risk state."""
        _aware(now)
        with self._lock, self._connection() as connection:
            batch = connection.execute(
                """SELECT owner_node_id, lease_generation, lineage_id,
                          expires_at, finalized
                   FROM allowance_batches WHERE batch_id = ?""",
                (batch_id,),
            ).fetchone()
            if batch is None or batch[0] != self._owner_node_id:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            highest = connection.execute(
                """SELECT max(lease_generation) FROM allowance_batches
                   WHERE owner_node_id = ? AND lineage_id = ?""",
                (self._owner_node_id, batch[2]),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT budget_scope_id, issued_amount, consumed_amount,
                          maximum_correction_risk
                   FROM allowance_leases WHERE batch_id = ? ORDER BY budget_scope_id""",
                (batch_id,),
            ).fetchall()
            return AllowanceState(
                batch_id,
                batch[1],
                datetime.fromisoformat(batch[3]),
                batch[1] == highest
                and now < datetime.fromisoformat(batch[3])
                and not batch[4],
                bool(batch[4]),
                tuple(
                    AllowanceScopeState(
                        row[0], Decimal(row[1]) - Decimal(row[2]), Decimal(row[3])
                    )
                    for row in rows
                ),
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


_APPLICABLE_SCOPE_SQL = """WITH RECURSIVE ancestors AS (
    SELECT id, parent_service_id FROM router.services WHERE id = %s
  UNION ALL
    SELECT service.id, service.parent_service_id FROM router.services AS service
    JOIN ancestors ON ancestors.parent_service_id = service.id
)
SELECT scope.id::text AS id, scope.currency::text AS currency
FROM router.budget_scopes AS scope
WHERE scope.scope_kind = 'global'
   OR (scope.scope_kind = 'service' AND scope.service_id IN (SELECT id FROM ancestors))
   OR (scope.scope_kind IN ('workspace', 'host_ceiling')
       AND scope.service_id = %s AND scope.workspace_id IS NOT DISTINCT FROM %s)
   OR (scope.scope_kind = 'assignment' AND scope.service_id = %s
       AND scope.assignment_id IS NOT DISTINCT FROM %s
       AND (scope.workspace_id IS NULL OR scope.workspace_id IS NOT DISTINCT FROM %s))
ORDER BY scope.id"""

_WALLET_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS allowance_batches (
    batch_id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    owner_node_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    service_id TEXT,
    workspace_id TEXT,
    assignment_id TEXT,
    currency TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    safety_until TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0 CHECK (finalized IN (0, 1))
);
CREATE TABLE IF NOT EXISTS allowance_leases (
    lease_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES allowance_batches(batch_id),
    budget_scope_id TEXT NOT NULL,
    issued_amount TEXT NOT NULL,
    maximum_correction_risk TEXT NOT NULL,
    consumed_amount TEXT NOT NULL,
    UNIQUE(batch_id, budget_scope_id)
);
"""


def _require_system(context: RequestContext, operation: str) -> None:
    if context.actor_kind is not PrincipalKind.SYSTEM or context.operation != operation:
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _require_node(context: RequestContext, operation: str, owner_node_id: str) -> None:
    _require_system(context, operation)
    if context.actor_id != owner_node_id:
        raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)


def _scope_shape(service_id: str | None, workspace_id: str | None) -> None:
    if workspace_id is not None and service_id is None:
        raise ValueError("A workspace allowance needs its service identity.")


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Allowance times must include a time zone.")


def _ordered_times(issued_at: datetime, expires_at: datetime, safety_until: datetime) -> None:
    _aware(issued_at)
    _aware(expires_at)
    _aware(safety_until)
    if not issued_at < expires_at <= safety_until:
        raise ValueError("Allowance times are not ordered.")

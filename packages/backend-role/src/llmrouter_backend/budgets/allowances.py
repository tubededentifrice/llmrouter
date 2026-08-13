"""Fenced central allowances and durable node-local consumption."""
# ruff: noqa: C901, D105, D107, E501, EM101, PLR0913, PLR0917, PLR2004, TRY003

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from json import dumps
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
            raise ValueError(
                "Allowance amounts must be positive with nonnegative risk."
            )
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
        if not all(
            (self.lease_id, self.batch_id, self.budget_scope_id, self.owner_node_id)
        ):
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
        if (
            scope_ids != set(self.applicable_scope_ids)
            or len(scope_ids) != len(self.applicable_scope_ids)
            or any(
                lease.batch_id != self.batch_id
                or lease.owner_node_id != self.owner_node_id
                or lease.lease_generation != self.lease_generation
                or lease.currency != currency
                or lease.issued_at != self.leases[0].issued_at
                or lease.expires_at != self.leases[0].expires_at
                or lease.safety_until != self.leases[0].safety_until
                for lease in self.leases
            )
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
        idempotency_key: str,
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
        if not 1 <= len(idempotency_key) <= 200:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        if not requested or len({item.budget_scope_id for item in requested}) != len(
            requested
        ):
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        if lineage_id is None and lease_generation != 1:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        fingerprint = _fingerprint(
            {
                "owner_node_id": owner_node_id,
                "lease_generation": lease_generation,
                "service_id": service_id,
                "workspace_id": workspace_id,
                "assignment_id": assignment_id,
                "currency": currency,
                "requests": [
                    [
                        item.budget_scope_id,
                        _decimal_text(item.amount),
                        _decimal_text(item.maximum_correction_risk),
                    ]
                    for item in requested
                ],
                "issued_at": issued_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "safety_until": safety_until.isoformat(),
                "lineage_id": lineage_id,
            }
        )
        batch_id = self._identity_factory()
        lineage = (
            self._identity_factory() if lineage_id is None else uuid.UUID(lineage_id)
        )
        leases: list[AllowanceLease] = []
        try:
            with self._connection() as connection, connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0))"
                )
                existing = connection.execute(
                    """SELECT id::text, request_fingerprint
                       FROM router.budget_allowance_batches
                       WHERE issuer_id = %s AND idempotency_key = %s""",
                    (context.actor_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise BudgetError(
                            BudgetErrorCode.IDEMPOTENCY_CONFLICT,
                            context.request_id,
                        )
                    return self._load_batch(connection, existing["id"])
                rows = connection.execute(
                    _APPLICABLE_SCOPE_SQL,
                    (
                        service_id,
                        service_id,
                        workspace_id,
                        service_id,
                        assignment_id,
                        workspace_id,
                    ),
                ).fetchall()
                applicable = {row["id"]: row["currency"] for row in rows}
                supplied = {item.budget_scope_id for item in requested}
                if supplied != set(applicable) or any(
                    applicable[item.budget_scope_id] != currency for item in requested
                ):
                    raise BudgetError(
                        BudgetErrorCode.INVALID_REQUEST, context.request_id
                    )
                connection.execute(
                    """INSERT INTO router.budget_allowance_batches (
                           id, issuer_id, idempotency_key, request_fingerprint,
                           lineage_id, owner_node_id, lease_generation, service_id,
                           workspace_id, assignment_id, currency, issued_at,
                           expires_at, safety_until
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                 %s, %s, %s, %s)""",
                    (
                        batch_id,
                        context.actor_id,
                        idempotency_key,
                        fingerprint,
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
            raise BudgetError(
                BudgetErrorCode.BUDGET_EXHAUSTED, context.request_id
            ) from error
        except psycopg.errors.SerializationFailure as error:
            raise BudgetError(
                BudgetErrorCode.STALE_ALLOWANCE, context.request_id
            ) from error
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
        batch_id: str,
        finals: Iterable[AllowanceFinal],
        *,
        reconciliation_id: str,
        now: datetime,
    ) -> bool:
        """Finalize one complete batch atomically, or return an exact replay."""
        _require_system(context, "budget.allowance.reconcile")
        _aware(now)
        reconciliation_uuid = uuid.UUID(reconciliation_id)
        supplied = tuple(finals)
        if not supplied or len({item.lease_id for item in supplied}) != len(supplied):
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        normalized = tuple(
            AllowanceFinal(
                item.lease_id,
                item.owner_node_id,
                item.lease_generation,
                exact_decimal(item.used_amount),
                exact_decimal(item.returned_amount),
            )
            for item in supplied
        )
        fingerprint = _final_fingerprint(batch_id, normalized, now, reclaimed=False)
        try:
            with self._connection() as connection, connection.transaction():
                batch = connection.execute(
                    """SELECT owner_node_id::text, lease_generation
                       FROM router.budget_allowance_batches WHERE id = %s FOR UPDATE""",
                    (batch_id,),
                ).fetchone()
                if batch is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                _require_node(
                    context,
                    "budget.allowance.reconcile",
                    batch["owner_node_id"],
                )
                existing = connection.execute(
                    """SELECT batch_id::text, request_fingerprint
                       FROM router.budget_allowance_batch_reconciliations
                       WHERE reconciliation_id = %s""",
                    (reconciliation_uuid,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["batch_id"] != batch_id
                        or existing["request_fingerprint"] != fingerprint
                    ):
                        raise BudgetError(
                            BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                        )
                    return True
                rows = connection.execute(
                    """SELECT id::text, budget_scope_id::text, issued_at
                       FROM router.budget_allowance_leases
                       WHERE batch_id = %s ORDER BY id FOR UPDATE""",
                    (batch_id,),
                ).fetchall()
                by_lease = {item.lease_id: item for item in normalized}
                if set(by_lease) != {row["id"] for row in rows} or any(
                    item.owner_node_id != batch["owner_node_id"]
                    or item.lease_generation != batch["lease_generation"]
                    for item in normalized
                ):
                    raise BudgetError(
                        BudgetErrorCode.STALE_ALLOWANCE, context.request_id
                    )
                connection.execute(
                    """INSERT INTO router.budget_allowance_batch_reconciliations (
                           reconciliation_id, batch_id, owner_node_id,
                           lease_generation, request_fingerprint, occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, %s, false)""",
                    (
                        reconciliation_uuid,
                        batch_id,
                        batch["owner_node_id"],
                        batch["lease_generation"],
                        fingerprint,
                        now,
                    ),
                )
                for row in rows:
                    final = by_lease[row["id"]]
                    lease_reconciliation_id = self._identity_factory()
                    connection.execute(
                        """INSERT INTO router.budget_allowance_reconciliations (
                           reconciliation_id, batch_reconciliation_id,
                           allowance_lease_id, owner_node_id, lease_generation,
                           used_amount, returned_amount, occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)""",
                        (
                            lease_reconciliation_id,
                            reconciliation_uuid,
                            final.lease_id,
                            final.owner_node_id,
                            final.lease_generation,
                            final.used_amount,
                            final.returned_amount,
                            now,
                        ),
                    )
                    self._insert_final_ledger(
                        connection,
                        final.lease_id,
                        row["budget_scope_id"],
                        lease_reconciliation_id,
                        final.used_amount,
                        final.returned_amount,
                        row["issued_at"],
                    )
        except psycopg.errors.UniqueViolation as error:
            raise BudgetError(
                BudgetErrorCode.STALE_ALLOWANCE, context.request_id
            ) from error
        except (
            psycopg.errors.SerializationFailure,
            psycopg.errors.CheckViolation,
        ) as error:
            raise BudgetError(
                BudgetErrorCode.STALE_ALLOWANCE, context.request_id
            ) from error
        return False

    def reclaim(
        self,
        context: RequestContext,
        *,
        batch_id: str,
        reconciliation_id: str,
        now: datetime,
    ) -> bool:
        """Reclaim one complete unreported batch after its safety window."""
        _require_system(context, "budget.allowance.reclaim")
        _aware(now)
        reconciliation_uuid = uuid.UUID(reconciliation_id)
        try:
            with self._connection() as connection, connection.transaction():
                batch = connection.execute(
                    """SELECT owner_node_id::text, lease_generation
                       FROM router.budget_allowance_batches WHERE id = %s FOR UPDATE""",
                    (batch_id,),
                ).fetchone()
                if batch is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                rows = connection.execute(
                    """SELECT id::text, budget_scope_id::text, issued_amount, issued_at
                       FROM router.budget_allowance_leases
                       WHERE batch_id = %s ORDER BY id FOR UPDATE""",
                    (batch_id,),
                ).fetchall()
                finals = tuple(
                    AllowanceFinal(
                        row["id"],
                        batch["owner_node_id"],
                        batch["lease_generation"],
                        row["issued_amount"],
                        Decimal(0),
                    )
                    for row in rows
                )
                fingerprint = _final_fingerprint(batch_id, finals, now, reclaimed=True)
                existing = connection.execute(
                    """SELECT batch_id::text, request_fingerprint
                       FROM router.budget_allowance_batch_reconciliations
                       WHERE reconciliation_id = %s""",
                    (reconciliation_uuid,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["batch_id"] != batch_id
                        or existing["request_fingerprint"] != fingerprint
                    ):
                        raise BudgetError(
                            BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                        )
                    return True
                connection.execute(
                    """INSERT INTO router.budget_allowance_batch_reconciliations (
                           reconciliation_id, batch_id, owner_node_id,
                           lease_generation, request_fingerprint, occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, %s, true)""",
                    (
                        reconciliation_uuid,
                        batch_id,
                        batch["owner_node_id"],
                        batch["lease_generation"],
                        fingerprint,
                        now,
                    ),
                )
                for row in rows:
                    lease_reconciliation_id = self._identity_factory()
                    connection.execute(
                        """INSERT INTO router.budget_allowance_reconciliations (
                           reconciliation_id, batch_reconciliation_id,
                           allowance_lease_id, owner_node_id, lease_generation,
                           used_amount, returned_amount, occurred_at, reclaimed
                       ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, true)""",
                        (
                            lease_reconciliation_id,
                            reconciliation_uuid,
                            row["id"],
                            batch["owner_node_id"],
                            batch["lease_generation"],
                            row["issued_amount"],
                            now,
                        ),
                    )
                    self._insert_final_ledger(
                        connection,
                        row["id"],
                        row["budget_scope_id"],
                        lease_reconciliation_id,
                        row["issued_amount"],
                        Decimal(0),
                        row["issued_at"],
                    )
        except (psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation) as error:
            raise BudgetError(
                BudgetErrorCode.STALE_ALLOWANCE, context.request_id
            ) from error
        return False

    def append_correction(
        self,
        context: RequestContext,
        *,
        lease_id: str,
        correction_id: str,
        amount_delta: Decimal,
        reason: str,
        now: datetime,
    ) -> bool:
        """Append one bounded late usage correction after final reconciliation."""
        _require_system(context, "budget.allowance.correct")
        delta = exact_decimal(amount_delta, signed=True)
        _aware(now)
        if not 1 <= len(reason) <= 500:
            raise BudgetError(BudgetErrorCode.INVALID_REQUEST, context.request_id)
        correction_uuid = uuid.UUID(correction_id)
        try:
            with self._connection() as connection, connection.transaction():
                lease = connection.execute(
                    """SELECT id FROM router.budget_allowance_leases
                       WHERE id = %s FOR UPDATE""",
                    (lease_id,),
                ).fetchone()
                if lease is None:
                    raise BudgetError(BudgetErrorCode.NOT_FOUND, context.request_id)
                existing = connection.execute(
                    """SELECT allowance_lease_id::text, amount_delta, reason, occurred_at
                       FROM router.budget_allowance_corrections
                       WHERE correction_id = %s""",
                    (correction_uuid,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["allowance_lease_id"],
                        existing["amount_delta"],
                        existing["reason"],
                        existing["occurred_at"],
                    ) != (lease_id, delta, reason, now):
                        raise BudgetError(
                            BudgetErrorCode.IDEMPOTENCY_CONFLICT, context.request_id
                        )
                    return True
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
                    (correction_uuid, lease_id, delta, reason, now),
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
                        correction_uuid,
                        row["issued_at"],
                    ),
                )
        except psycopg.errors.CheckViolation as error:
            raise BudgetError(
                BudgetErrorCode.INVALID_REQUEST, context.request_id
            ) from error
        return False

    def _load_batch(
        self,
        connection: Connection[DictRow],
        batch_id: str,
    ) -> AllowanceBatch:
        batch = connection.execute(
            """SELECT id::text, lineage_id::text, owner_node_id::text,
                      lease_generation, service_id::text, workspace_id::text,
                      assignment_id::text, currency::text
               FROM router.budget_allowance_batches WHERE id = %s""",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise RuntimeError("The allowance batch disappeared during replay.")
        rows = connection.execute(
            """SELECT id::text, budget_scope_id::text, owner_node_id::text,
                      lease_generation, currency::text, issued_amount,
                      maximum_correction_risk, issued_at, expires_at, safety_until
               FROM router.budget_allowance_leases
               WHERE batch_id = %s ORDER BY budget_scope_id""",
            (batch_id,),
        ).fetchall()
        leases = tuple(
            AllowanceLease(
                row["id"],
                batch["id"],
                row["budget_scope_id"],
                row["owner_node_id"],
                row["lease_generation"],
                row["currency"],
                row["issued_amount"],
                row["maximum_correction_risk"],
                row["issued_at"],
                row["expires_at"],
                row["safety_until"],
            )
            for row in rows
        )
        return AllowanceBatch(
            batch["id"],
            batch["lineage_id"],
            batch["owner_node_id"],
            batch["lease_generation"],
            batch["service_id"],
            batch["workspace_id"],
            batch["assignment_id"],
            batch["currency"],
            tuple(row["budget_scope_id"] for row in rows),
            leases,
        )

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

    def __init__(
        self,
        path: str | Path,
        *,
        owner_node_id: str,
        authority_id: str,
    ) -> None:
        if not owner_node_id or not authority_id:
            raise ValueError("The node and authority identities must not be empty.")
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("An allowance wallet must use durable storage.")
        self._owner_node_id = owner_node_id
        self._authority_id = authority_id
        self._lock = threading.Lock()
        with self._connection() as connection:
            connection.executescript(_WALLET_SCHEMA)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(allowance_batches)")
            }
            if "last_observed_at" not in columns:
                connection.execute(
                    "ALTER TABLE allowance_batches ADD COLUMN last_observed_at TEXT"
                )

    def install(self, context: RequestContext, batch: AllowanceBatch) -> None:
        """Persist a trusted complete batch without restoring spent value."""
        _require_system(context, "budget.allowance.install")
        if context.actor_id != self._authority_id:
            raise BudgetError(BudgetErrorCode.INSUFFICIENT_SCOPE, context.request_id)
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
                if (
                    lease.batch_id != batch.batch_id
                    or lease.owner_node_id != batch.owner_node_id
                ):
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
                          workspace_id, assignment_id, issued_at, expires_at,
                          finalized, last_observed_at
                   FROM allowance_batches WHERE batch_id = ?""",
                (batch_id,),
            ).fetchone()
            if batch is None or batch[0] != self._owner_node_id:
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if batch[7]:
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
            if now < datetime.fromisoformat(batch[5]):
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if batch[8] is not None and now <= datetime.fromisoformat(batch[8]):
                raise BudgetError(BudgetErrorCode.STALE_ALLOWANCE, batch_id)
            if now >= datetime.fromisoformat(batch[6]):
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
            connection.execute(
                "UPDATE allowance_batches SET last_observed_at = ? WHERE batch_id = ?",
                (now.isoformat(), batch_id),
            )
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
                          issued_at, expires_at, finalized, last_observed_at
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
                datetime.fromisoformat(batch[4]),
                batch[1] == highest
                and now >= datetime.fromisoformat(batch[3])
                and now < datetime.fromisoformat(batch[4])
                and (batch[6] is None or now >= datetime.fromisoformat(batch[6]))
                and not batch[5],
                bool(batch[5]),
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
    last_observed_at TEXT,
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


def _ordered_times(
    issued_at: datetime, expires_at: datetime, safety_until: datetime
) -> None:
    _aware(issued_at)
    _aware(expires_at)
    _aware(safety_until)
    if not issued_at < expires_at <= safety_until:
        raise ValueError("Allowance times are not ordered.")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _fingerprint(value: object) -> bytes:
    return sha256(
        dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _final_fingerprint(
    batch_id: str,
    finals: Iterable[AllowanceFinal],
    now: datetime,
    *,
    reclaimed: bool,
) -> bytes:
    return _fingerprint(
        {
            "batch_id": batch_id,
            "finals": [
                [
                    item.lease_id,
                    item.owner_node_id,
                    item.lease_generation,
                    _decimal_text(item.used_amount),
                    _decimal_text(item.returned_amount),
                ]
                for item in sorted(finals, key=lambda value: value.lease_id)
            ],
            "occurred_at": now.isoformat(),
            "reclaimed": reclaimed,
        }
    )

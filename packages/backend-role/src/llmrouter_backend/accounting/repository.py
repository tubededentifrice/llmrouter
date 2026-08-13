"""PostgreSQL accounting, price synchronization, and correction operations."""
# ruff: noqa: C901, E501, EM101, FBT001, FBT003, PLR0911, PLR0912, PLR0913, PLR0915, PLR0917, PLR2004, TRY003

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.pq import TransactionStatus

from llmrouter_backend.accounting.errors import AccountingError
from llmrouter_backend.accounting.model import (
    AccountingCorrection,
    AccountingEvent,
    AccountingSummary,
    PriceComponent,
    RawPriceComponent,
    SourceSnapshot,
    SourceSnapshotEvidence,
    SynchronizationResult,
    SynchronizationRow,
    SynchronizationRunState,
    SynchronizationState,
    SynchronizationStatus,
    UsageComponent,
    UsageDelta,
    UsageUnit,
    exact_decimal,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class PostgresAccountingRepository:
    """Keep exact immutable accounting and price evidence."""

    def __init__(self, database_url: str) -> None:
        """Use the given PostgreSQL connection URL."""
        self._database_url = database_url

    def ingest(self, context: RequestContext, event: AccountingEvent) -> bool:
        """Insert one exact canonical accounting event or accept an equal replay."""
        self._require_system(context, "accounting.ingest")
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                for identity in sorted((event.event_id, event.canonical_event_id)):
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"accounting-fact:{identity}",),
                    )
                existing = connection.execute(
                    """
                    SELECT canonical_event_id::text, request_row_id::text,
                           service_id::text, workspace_id::text,
                           budget_scope_id::text, subject_kind, subject_id::text,
                           outcome, currency::text, price_version_id::text,
                           amount, occurred_at, canonical_payload_sha256,
                           assignment_id::text, budget_ledger_event_id::text
                    FROM router.accounting_facts
                    WHERE event_id = %s OR canonical_event_id = %s
                    """,
                    (event.event_id, event.canonical_event_id),
                ).fetchone()
                amount = self._event_amount(connection, event)
                expected = (
                    event.canonical_event_id,
                    event.request_row_id,
                    event.service_id,
                    event.workspace_id,
                    event.budget_scope_id,
                    event.subject_kind.value,
                    event.subject_id,
                    event.outcome.value,
                    event.currency,
                    event.price_version_id,
                    amount,
                    event.occurred_at,
                    event.canonical_payload_sha256(),
                    event.assignment_id,
                    event.budget_ledger_event_id,
                )
                if existing is not None:
                    if (
                        str(existing[0]) != event.canonical_event_id
                        or tuple(existing) != expected
                        or self._usage(connection, event.event_id) != event.usage
                    ):
                        raise AccountingError(
                            "The accounting event identity has different content."
                        )
                    return True
                connection.execute(
                    """
                    INSERT INTO router.accounting_facts (
                        event_id, canonical_event_id, request_row_id, service_id,
                        workspace_id, budget_scope_id, subject_kind, subject_id,
                        outcome, currency, price_version_id, amount, occurred_at,
                        canonical_payload_sha256, assignment_id,
                        budget_ledger_event_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.event_id,
                        event.canonical_event_id,
                        event.request_row_id,
                        event.service_id,
                        event.workspace_id,
                        event.budget_scope_id,
                        event.subject_kind.value,
                        event.subject_id,
                        event.outcome.value,
                        event.currency,
                        event.price_version_id,
                        amount,
                        event.occurred_at,
                        event.canonical_payload_sha256(),
                        event.assignment_id,
                        event.budget_ledger_event_id,
                    ),
                )
                for component in event.usage:
                    connection.execute(
                        """
                        INSERT INTO router.accounting_usage_components (
                            event_id, unit_name, quantity
                        ) VALUES (%s, %s, %s)
                        """,
                        (event.event_id, component.unit.value, component.quantity),
                    )
            self._require_commit(connection)
        return False

    def append_correction(
        self, context: RequestContext, correction: AccountingCorrection
    ) -> bool:
        """Append a signed correction without changing its source event."""
        self._require_system(context, "accounting.correct")
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (correction.correction_id,),
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"accounting-source:{correction.source_event_id}",),
                )
                existing = connection.execute(
                    """
                    SELECT source_event_id::text, correction_kind, currency::text,
                           amount_delta, source_name, reason, occurred_at
                    FROM router.accounting_corrections WHERE correction_id = %s
                    """,
                    (correction.correction_id,),
                ).fetchone()
                expected = (
                    correction.source_event_id,
                    correction.kind.value,
                    correction.currency,
                    correction.amount_delta,
                    correction.source,
                    correction.reason,
                    correction.occurred_at,
                )
                if existing is not None:
                    if (
                        tuple(existing) != expected
                        or self._correction_usage(connection, correction.correction_id)
                        != correction.usage_delta
                    ):
                        raise AccountingError(
                            "The correction identity has different content."
                        )
                    return True
                self._check_correction_usage(connection, correction)
                connection.execute(
                    """
                    INSERT INTO router.accounting_corrections (
                        correction_id, source_event_id, correction_kind, currency,
                        amount_delta, source_name, reason, occurred_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (correction.correction_id, *expected),
                )
                for component in correction.usage_delta:
                    connection.execute(
                        """
                        INSERT INTO router.accounting_correction_usage (
                            correction_id, unit_name, quantity_delta
                        ) VALUES (%s, %s, %s)
                        """,
                        (
                            correction.correction_id,
                            component.unit.value,
                            component.quantity,
                        ),
                    )
            self._require_commit(connection)
        return False

    def synchronize(
        self,
        context: RequestContext,
        *,
        service_id: str | None,
        snapshot: SourceSnapshot,
        route_ids: Sequence[str],
        dry_run: bool,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> SynchronizationResult:
        """Validate one immutable source snapshot and atomically accept good rows."""
        self._require_sync(context, service_id)
        self._require_aware_time(now)
        if idempotency_key is not None and not 16 <= len(idempotency_key) <= 200:
            raise AccountingError(
                "The idempotency key must contain from 16 to 200 characters."
            )
        operation_id = str(uuid.uuid4())
        snapshot_id = str(uuid.uuid4())
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "dry_run": dry_run,
                    "route_ids": sorted(route_ids),
                    "service_id": service_id,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "http_validator": snapshot.http_validator,
                    "snapshot_digest": snapshot.content_sha256,
                    "source_name": snapshot.source_name,
                    "source_revision": snapshot.source_revision,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).digest()
        result_rows: list[SynchronizationRow] = []
        revision_id: str | None = None
        revision_ids: tuple[str, ...] = ()
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                if idempotency_key is not None:
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"price-sync:{context.actor_id}:{idempotency_key}",),
                    )
                    replay = connection.execute(
                        """SELECT run_id::text, request_fingerprint
                           FROM router.price_synchronization_idempotency
                           WHERE actor_id = %s AND idempotency_key = %s""",
                        (context.actor_id, idempotency_key),
                    ).fetchone()
                    if replay is not None:
                        if replay[1] != request_fingerprint:
                            raise AccountingError(
                                "The idempotency key has different synchronization content."
                            )
                        pending_rows = connection.execute(
                            """SELECT service_id::text, state
                               FROM router.price_publication_outbox
                               WHERE synchronization_run_id = %s
                               ORDER BY service_id NULLS FIRST FOR UPDATE""",
                            (replay[0],),
                        ).fetchall()
                        for pending in pending_rows:
                            if pending[1] != "pending":
                                continue
                            self._require_publication(context, pending[0])
                            replay_revision = self._publish_price_revision(
                                connection, pending[0], context, replay[0], now
                            )
                            connection.execute(
                                """UPDATE router.price_publication_outbox
                                   SET state = 'published',
                                       resulting_configuration_revision_id = %s,
                                       published_at = %s
                                   WHERE synchronization_run_id = %s
                                     AND service_id IS NOT DISTINCT FROM %s""",
                                (replay_revision, now, replay[0], pending[0]),
                            )
                            connection.execute(
                                """INSERT INTO router.price_synchronization_publications (
                                       synchronization_run_id, service_id,
                                       configuration_revision_id
                                   ) VALUES (%s, %s, %s)""",
                                (replay[0], pending[0], replay_revision),
                            )
                        publications = connection.execute(
                            """SELECT configuration_revision_id::text
                               FROM router.price_synchronization_publications
                               WHERE synchronization_run_id = %s""",
                            (replay[0],),
                        ).fetchall()
                        if len(publications) == 1:
                            connection.execute(
                                """UPDATE router.price_synchronization_runs
                                   SET resulting_configuration_revision_id = %s
                                   WHERE id = %s
                                     AND resulting_configuration_revision_id IS NULL""",
                                (publications[0][0], replay[0]),
                            )
                        return self._synchronization_result(connection, replay[0])
                routes = self._price_routes(
                    connection, service_id, snapshot.source_name, route_ids
                )
                if not dry_run:
                    for route_id in sorted(row[0] for row in routes):
                        connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"price-version:{route_id}",),
                        )
                result_rows.extend(
                    self._normalize_price_row(connection, route, snapshot, now)
                    for route in routes
                )
                if dry_run:
                    result_rows = [
                        replace(row, price_version_id=None) for row in result_rows
                    ]
                connection.execute(
                    """INSERT INTO router.price_source_snapshots (
                           id, source_name, fetched_at, source_revision,
                           content_sha256, http_validator, source_available
                       ) VALUES (%s, %s, %s, %s, decode(%s, 'hex'), %s, %s)""",
                    (
                        snapshot_id,
                        snapshot.source_name,
                        snapshot.fetched_at,
                        snapshot.source_revision,
                        snapshot.content_sha256,
                        snapshot.http_validator,
                        snapshot.source_available,
                    ),
                )
                if not dry_run:
                    for row in result_rows:
                        if row.status is SynchronizationStatus.UPDATED:
                            self._insert_price_version(
                                connection, row, snapshot_id, now
                            )
                        self._persist_synchronization_state(connection, row)
                    needs_publication = any(
                        row.status is SynchronizationStatus.UPDATED
                        for row in result_rows
                    )
                connection.execute(
                    """
                    INSERT INTO router.price_synchronization_runs (
                        id, service_id, source_name, source_snapshot_id, dry_run,
                        state, resulting_configuration_revision_id, started_at,
                        completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        operation_id,
                        service_id,
                        snapshot.source_name,
                        snapshot_id,
                        dry_run,
                        "previewed" if dry_run else "completed",
                        revision_id,
                        now,
                        None if dry_run else now,
                    ),
                )
                if idempotency_key is not None:
                    connection.execute(
                        """INSERT INTO router.price_synchronization_idempotency (
                               actor_id, idempotency_key, request_fingerprint, run_id
                           ) VALUES (%s, %s, %s, %s)""",
                        (
                            context.actor_id,
                            idempotency_key,
                            request_fingerprint,
                            operation_id,
                        ),
                    )
                if not dry_run and needs_publication:
                    owner_scopes = {
                        route[4] if route[3] == "service" else None
                        for route in routes
                        if any(
                            item.provider_model_route_id == route[0]
                            and item.status is SynchronizationStatus.UPDATED
                            for item in result_rows
                        )
                    }
                    for owner_service_id in sorted(
                        owner_scopes, key=lambda value: value or ""
                    ):
                        connection.execute(
                            """INSERT INTO router.price_publication_outbox (
                                   synchronization_run_id, service_id, state,
                                   accepted_at
                               ) VALUES (%s, %s, 'pending', %s)""",
                            (operation_id, owner_service_id, now),
                        )
                for row in result_rows:
                    connection.execute(
                        """
                        INSERT INTO router.price_synchronization_results (
                            run_id, provider_model_route_id, lookup_identifier,
                            status, synchronization_state, old_prices, new_prices,
                            price_version_id, error_class, synchronized_at
                        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                                  %s, %s, %s)
                        """,
                        (
                            operation_id,
                            row.provider_model_route_id,
                            row.lookup_identifier,
                            row.status.value,
                            row.synchronization_state.value,
                            self._price_json(row.old_prices),
                            self._price_json(row.new_prices),
                            row.price_version_id,
                            row.error_class,
                            now,
                        ),
                    )
                self._insert_sync_audit(
                    connection,
                    context,
                    service_id,
                    operation_id,
                    result_rows,
                    dry_run,
                    now,
                )
            self._require_commit(connection)
        if not dry_run and any(
            row.status is SynchronizationStatus.UPDATED for row in result_rows
        ):
            revision_ids = self.publish_all_pending(context, operation_id, now=now)
            revision_id = revision_ids[0] if len(revision_ids) == 1 else None
        return SynchronizationResult(
            operation_id,
            dry_run,
            snapshot_id,
            tuple(result_rows),
            revision_id,
            revision_ids,
            SynchronizationRunState.PREVIEWED
            if dry_run
            else SynchronizationRunState.COMPLETED,
            SourceSnapshotEvidence(
                snapshot.source_name,
                snapshot.fetched_at,
                snapshot.content_sha256,
                snapshot.source_revision,
                snapshot.http_validator,
            ),
        )

    def publish_pending(
        self, context: RequestContext, operation_id: str, *, now: datetime
    ) -> str:
        """Publish one single-scope price update."""
        revisions = self.publish_all_pending(context, operation_id, now=now)
        if len(revisions) != 1:
            raise AccountingError("The price publication has multiple owner scopes.")
        return revisions[0]

    def publish_pending_operations(
        self,
        context: RequestContext,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Publish a bounded batch of durable pending synchronization operations."""
        self._require_system(context, "price.publish")
        self._require_aware_time(now)
        if not 1 <= limit <= 1000:
            raise AccountingError("The publication batch limit is invalid.")
        with psycopg.connect(self._database_url) as connection:
            operations = connection.execute(
                """SELECT synchronization_run_id::text
                   FROM router.price_publication_outbox
                   WHERE state = 'pending'
                   GROUP BY synchronization_run_id
                   ORDER BY min(accepted_at), synchronization_run_id
                   LIMIT %s""",
                (limit,),
            ).fetchall()
        operation_ids = tuple(row[0] for row in operations)
        for operation_id in operation_ids:
            self.publish_all_pending(context, operation_id, now=now)
        return operation_ids

    def publish_all_pending(
        self, context: RequestContext, operation_id: str, *, now: datetime
    ) -> tuple[str, ...]:
        """Publish all owner-scope revisions for one committed price update."""
        self._require_aware_time(now)
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    SELECT service_id::text, state,
                           resulting_configuration_revision_id::text
                    FROM router.price_publication_outbox
                    WHERE synchronization_run_id = %s
                    ORDER BY service_id NULLS FIRST FOR UPDATE
                    """,
                    (operation_id,),
                ).fetchall()
                if not rows:
                    raise AccountingError("The price publication is not pending.")
                revision_values: list[str] = []
                for row in rows:
                    self._require_publication(context, row[0])
                    if row[1] == "published":
                        if row[2] is None:
                            raise AccountingError(
                                "The price publication is incomplete."
                            )
                        revision_values.append(str(row[2]))
                        continue
                    published = self._publish_price_revision(
                        connection, row[0], context, operation_id, now
                    )
                    revision_values.append(published)
                    connection.execute(
                        """UPDATE router.price_publication_outbox
                           SET state = 'published',
                               resulting_configuration_revision_id = %s,
                               published_at = %s
                           WHERE synchronization_run_id = %s
                             AND service_id IS NOT DISTINCT FROM %s""",
                        (published, now, operation_id, row[0]),
                    )
                    connection.execute(
                        """INSERT INTO router.price_synchronization_publications (
                               synchronization_run_id, service_id,
                               configuration_revision_id
                           ) VALUES (%s, %s, %s)""",
                        (operation_id, row[0], published),
                    )
                if len(revision_values) == 1:
                    connection.execute(
                        """UPDATE router.price_synchronization_runs
                           SET resulting_configuration_revision_id = %s
                           WHERE id = %s""",
                        (revision_values[0], operation_id),
                    )
            self._require_commit(connection)
        return tuple(revision_values)

    def get_synchronization(
        self, context: RequestContext, operation_id: str
    ) -> SynchronizationResult:
        """Return one durable synchronization result within its authority scope."""
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                "SELECT service_id::text FROM router.price_synchronization_runs WHERE id = %s",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise AccountingError("The price synchronization does not exist.")
            self._require_sync_read(context, row[0])
            return self._synchronization_result(connection, operation_id)

    def due_synchronizations(
        self, context: RequestContext, *, now: datetime
    ) -> tuple[tuple[str | None, str, tuple[str, ...]], ...]:
        """Return due source groups and mark aged accepted prices stale."""
        self._require_system(context, "price.schedule")
        self._require_aware_time(now)
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                connection.execute(
                    """UPDATE router.route_price_synchronization_states AS state
                       SET synchronization_state = 'stale'
                       FROM router.route_price_sources AS source
                       WHERE source.provider_model_route_id = state.provider_model_route_id
                         AND source.authority_kind = 'synchronized'
                         AND state.synchronization_state = 'current'
                         AND state.observed_at + source.stale_after <= %s""",
                    (now,),
                )
                rows = connection.execute(
                    """SELECT route.owner_service_id::text, source.source_name,
                              source.synchronization_schedule,
                              state.observed_at, route.id::text
                       FROM router.route_price_sources AS source
                       JOIN router.provider_model_routes AS route
                         ON route.id = source.provider_model_route_id
                       LEFT JOIN router.route_price_synchronization_states AS state
                         ON state.provider_model_route_id = route.id
                       WHERE source.authority_kind = 'synchronized'
                         AND route.state = 'active'
                         AND source.synchronization_schedule IS NOT NULL
                       ORDER BY route.owner_service_id NULLS FIRST,
                                source.source_name, route.id"""
                ).fetchall()
            self._require_commit(connection)
        groups: dict[tuple[str | None, str], list[str]] = {}
        for owner, source, schedule, observed_at, route_id in rows:
            current_minute = now.replace(second=0, microsecond=0)
            due = observed_at is None or (
                observed_at < current_minute and self._cron_matches(schedule, now)
            )
            if due:
                groups.setdefault((owner, source), []).append(route_id)
        return tuple(
            (owner, source, tuple(route_ids))
            for (owner, source), route_ids in sorted(
                groups.items(), key=lambda item: ((item[0][0] or ""), item[0][1])
            )
        )

    @staticmethod
    def _cron_matches(schedule: str, now: datetime) -> bool:
        values = (now.minute, now.hour, now.day, now.month, (now.weekday() + 1) % 7)

        def matches(field: str, value: int) -> bool:
            for item in field.split(","):
                base, separator, step_text = item.partition("/")
                step = int(step_text) if separator else 1
                if base == "*":
                    if value % step == 0:
                        return True
                    continue
                start_text, range_separator, end_text = base.partition("-")
                start = int(start_text)
                end = int(end_text) if range_separator else start
                if start <= value <= end and (value - start) % step == 0:
                    return True
            return False

        return all(
            matches(field, value)
            for field, value in zip(schedule.split(), values, strict=True)
        )

    def summary(
        self,
        context: RequestContext,
        scope: Scope,
        *,
        start: datetime,
        end: datetime,
    ) -> AccountingSummary:
        """Return one isolated exact aggregate without captured content."""
        self._require_read(context, scope)
        self._require_time_range(start, end)
        where, params = self._scope_filter(scope)
        with psycopg.connect(self._database_url) as connection:
            fact_where = where.replace("service_id", "fact.service_id").replace(
                "workspace_id", "fact.workspace_id"
            )
            currencies = connection.execute(
                f"""SELECT DISTINCT currency FROM (
                         SELECT fact.currency::text AS currency
                         FROM router.accounting_facts AS fact
                         WHERE {fact_where} AND fact.occurred_at >= %s AND fact.occurred_at < %s
                         UNION
                         SELECT correction.currency::text
                         FROM router.accounting_corrections AS correction
                         JOIN router.accounting_facts AS fact
                           ON fact.event_id = correction.source_event_id
                         WHERE {fact_where} AND correction.occurred_at >= %s
                           AND correction.occurred_at < %s
                     ) AS ranged""",  # noqa: S608  # nosec B608 - Fixed clauses.
                (*params, start, end, *params, start, end),
            ).fetchall()
            if len(currencies) > 1:
                raise AccountingError("The accounting range contains mixed currencies.")
            currency = (
                currencies[0][0]
                if currencies
                else self._scope_currency(connection, scope)
            )
            totals = connection.execute(
                f"""
                SELECT count(DISTINCT request_row_id),
                       count(*) FILTER (WHERE subject_kind <> 'logical_request'),
                       COALESCE(sum(amount), 0)
                FROM router.accounting_facts
                WHERE {where} AND occurred_at >= %s AND occurred_at < %s
                """,  # noqa: S608  # nosec B608 - Fixed clauses.
                (*params, start, end),
            ).fetchone()
            usage_rows = connection.execute(
                f"""
                WITH usage AS (
                    SELECT item.unit_name, item.quantity
                    FROM router.accounting_usage_components AS item
                    JOIN router.accounting_facts AS fact
                      ON fact.event_id = item.event_id
                    WHERE {fact_where}
                      AND fact.occurred_at >= %s AND fact.occurred_at < %s
                    UNION ALL
                    SELECT item.unit_name, item.quantity_delta
                    FROM router.accounting_correction_usage AS item
                    JOIN router.accounting_corrections AS correction
                      ON correction.correction_id = item.correction_id
                    JOIN router.accounting_facts AS fact
                      ON fact.event_id = correction.source_event_id
                    WHERE {fact_where}
                      AND correction.occurred_at >= %s
                      AND correction.occurred_at < %s
                )
                SELECT unit_name, sum(quantity)
                FROM usage GROUP BY unit_name ORDER BY unit_name
                """,  # noqa: S608  # nosec B608 - Fixed clauses.
                (*params, start, end, *params, start, end),
            ).fetchall()
            correction = connection.execute(
                f"""
                SELECT COALESCE(sum(correction.amount_delta), 0)
                FROM router.accounting_corrections AS correction
                JOIN router.accounting_facts AS fact
                  ON fact.event_id = correction.source_event_id
                WHERE {fact_where}
                  AND correction.occurred_at >= %s AND correction.occurred_at < %s
                """,  # noqa: S608  # nosec B608 - Fixed clauses.
                (*params, start, end),
            ).fetchone()
        if totals is None or correction is None:
            raise AccountingError("The accounting aggregate query failed.")
        if any(row[1] < 0 for row in usage_rows):
            raise AccountingError(
                "Accounting corrections make the reported usage negative."
            )
        return AccountingSummary(
            currency,
            int(totals[0]),
            int(totals[1]),
            tuple(UsageComponent(UsageUnit(row[0]), row[1]) for row in usage_rows),
            totals[2],
            correction[0],
        )

    def rebuild_daily_aggregates(
        self,
        context: RequestContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Reproduce daily aggregates from retained immutable facts."""
        self._require_system(context, "accounting.aggregate")
        if start is not None:
            self._require_aware_time(start)
        if end is not None:
            self._require_aware_time(end)
        if start is not None and end is not None and start >= end:
            raise AccountingError("The operation time range is invalid.")
        with psycopg.connect(self._database_url) as connection:
            with connection.transaction():
                bounds = connection.execute(
                    """SELECT min(occurred_at), max(occurred_at) FROM (
                           SELECT occurred_at FROM router.accounting_facts
                           UNION ALL SELECT occurred_at FROM router.accounting_corrections
                       ) AS retained"""
                ).fetchone()
                lower = start or (bounds[0] if bounds else None)
                upper = end or (
                    (bounds[1] + timedelta(microseconds=1))
                    if bounds and bounds[1]
                    else None
                )
                if lower is None or upper is None:
                    return 0
                lower = lower.astimezone(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                upper = upper.astimezone(UTC)
                upper_day = upper.replace(hour=0, minute=0, second=0, microsecond=0)
                if upper != upper_day:
                    upper = upper_day + timedelta(days=1)
                connection.execute(
                    """DELETE FROM router.daily_accounting_aggregates
                       WHERE accounting_day >= (%s AT TIME ZONE 'UTC')::date
                         AND accounting_day < (%s AT TIME ZONE 'UTC')::date
                             + CASE WHEN (%s AT TIME ZONE 'UTC')::time = time '00:00'
                                    THEN 0 ELSE 1 END""",
                    (lower, upper, upper),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO router.daily_accounting_aggregates (
                        accounting_day, service_id, workspace_id, currency,
                        logical_requests, attempts, cost, corrections, usage
                    )
                    WITH facts AS (
                        SELECT (fact.occurred_at AT TIME ZONE 'UTC')::date AS accounting_day,
                               fact.service_id, fact.workspace_id, fact.currency,
                               count(DISTINCT fact.request_row_id) AS logical_requests,
                               count(*) FILTER (
                                   WHERE fact.subject_kind <> 'logical_request'
                               ) AS attempts,
                               sum(fact.amount) AS cost,
                               0::numeric AS corrections
                        FROM router.accounting_facts AS fact
                        WHERE fact.occurred_at >= %s AND fact.occurred_at < %s
                        GROUP BY (fact.occurred_at AT TIME ZONE 'UTC')::date, fact.service_id,
                                 fact.workspace_id, fact.currency
                    ), corrections AS (
                        SELECT (correction.occurred_at AT TIME ZONE 'UTC')::date AS accounting_day,
                               fact.service_id, fact.workspace_id, fact.currency,
                               0::bigint AS logical_requests,
                               0::bigint AS attempts, 0::numeric AS cost,
                               sum(correction.amount_delta) AS corrections
                        FROM router.accounting_corrections AS correction
                        JOIN router.accounting_facts AS fact
                          ON fact.event_id = correction.source_event_id
                        WHERE correction.occurred_at >= %s AND correction.occurred_at < %s
                        GROUP BY (correction.occurred_at AT TIME ZONE 'UTC')::date, fact.service_id,
                                 fact.workspace_id, fact.currency
                    ), entries AS (
                        SELECT * FROM facts UNION ALL SELECT * FROM corrections
                    )
                    SELECT entry.accounting_day, entry.service_id,
                           entry.workspace_id, entry.currency,
                           sum(logical_requests), sum(attempts), sum(cost),
                           sum(corrections),
                           COALESCE((
                               SELECT jsonb_object_agg(value.unit_name, value.quantity)
                               FROM (
                                   SELECT unit_name, sum(quantity) AS quantity
                                   FROM (
                                       SELECT original.unit_name,
                                              original.quantity
                                       FROM router.accounting_usage_components
                                            AS original
                                       JOIN router.accounting_facts AS source
                                         ON source.event_id = original.event_id
                                       WHERE (source.occurred_at AT TIME ZONE 'UTC')::date =
                                             entry.accounting_day
                                         AND source.service_id = entry.service_id
                                         AND source.workspace_id IS NOT DISTINCT
                                             FROM entry.workspace_id
                                         AND source.currency = entry.currency
                                       UNION ALL
                                       SELECT delta.unit_name,
                                              delta.quantity_delta
                                       FROM router.accounting_correction_usage
                                            AS delta
                                       JOIN router.accounting_corrections AS change
                                         ON change.correction_id =
                                            delta.correction_id
                                       JOIN router.accounting_facts AS source
                                         ON source.event_id = change.source_event_id
                                       WHERE (change.occurred_at AT TIME ZONE 'UTC')::date =
                                             entry.accounting_day
                                         AND source.service_id = entry.service_id
                                         AND source.workspace_id IS NOT DISTINCT
                                             FROM entry.workspace_id
                                         AND source.currency = entry.currency
                                   ) AS quantities
                                   GROUP BY unit_name
                               ) AS value
                           ), '{}'::jsonb)
                    FROM entries AS entry
                    GROUP BY entry.accounting_day, entry.service_id,
                             entry.workspace_id, entry.currency
                    """,
                    (lower, upper, lower, upper),
                )
            self._require_commit(connection)
            return cursor.rowcount

    @staticmethod
    def _require_commit(connection: psycopg.Connection[Any]) -> None:
        if connection.info.transaction_status is not TransactionStatus.IDLE:
            raise AccountingError("The accounting transaction did not commit.")

    @staticmethod
    def _require_system(context: RequestContext, operation: str) -> None:
        if not (
            context.actor_kind is PrincipalKind.SYSTEM
            and context.authority_class is AuthorityClass.SYSTEM
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is None
            and context.scope == Scope()
            and context.operation == operation
            and context.mutation
        ):
            raise AccountingError("The accounting operation is not authorized.")

    @staticmethod
    def _require_read(context: RequestContext, scope: Scope) -> None:
        if (
            context.scope != scope
            or context.operation != "accounting.read"
            or context.mutation
        ):
            raise AccountingError("The accounting read is not authorized.")
        machine = (
            context.actor_kind is PrincipalKind.SERVICE
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is Audience.ACCOUNTING
            and context.actor_id == scope.service_id
        )
        embed = (
            context.actor_kind is PrincipalKind.EMBED
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.EMBED
            and context.machine_audience is None
        )
        administrator = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
        )
        if not (machine or embed or administrator):
            raise AccountingError("The accounting read is not authorized.")

    @staticmethod
    def _require_sync(context: RequestContext, service_id: str | None) -> None:
        expected_scope = Scope(service_id) if service_id else Scope()
        if context.scope != expected_scope or not context.mutation:
            raise AccountingError("The price synchronization is not authorized.")
        service = (
            service_id is not None
            and context.actor_kind is PrincipalKind.SERVICE
            and context.actor_id == service_id
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is Audience.CONFIGURATION
            and context.operation == "configuration.write"
        )
        administrator = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.machine_audience is None
            and context.operation == "provider_route.manage"
        )
        embed = (
            service_id is not None
            and context.actor_kind is PrincipalKind.EMBED
            and context.authority_class is AuthorityClass.SERVICE
            and context.authority_path is AuthorityPath.EMBED
            and context.machine_audience is None
            and context.operation == "configuration.write"
        )
        if not (service or embed or administrator):
            raise AccountingError("The price synchronization is not authorized.")

    @staticmethod
    def _require_sync_read(context: RequestContext, service_id: str | None) -> None:
        read_context = RequestContext(
            context.request_id,
            context.actor_kind,
            context.actor_id,
            context.authority_class,
            context.authority_path,
            context.machine_audience,
            context.operation,
            context.scope,
            context.authorized_at,
            context.recent_authentication_at,
            True,
        )
        PostgresAccountingRepository._require_sync(read_context, service_id)

    @staticmethod
    def _require_publication(context: RequestContext, service_id: str | None) -> None:
        system = (
            context.actor_kind is PrincipalKind.SYSTEM
            and context.authority_class is AuthorityClass.SYSTEM
            and context.authority_path is AuthorityPath.MACHINE
            and context.machine_audience is None
            and context.scope == Scope()
            and context.operation == "price.publish"
            and context.mutation
        )
        global_administrator = (
            context.actor_kind is PrincipalKind.ADMINISTRATOR
            and context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            and context.authority_path is AuthorityPath.GLOBAL_ADMINISTRATION
            and context.machine_audience is None
            and context.scope == Scope()
            and context.operation == "provider_route.manage"
            and context.mutation
        )
        if not (system or global_administrator):
            PostgresAccountingRepository._require_sync(context, service_id)

    @staticmethod
    def _usage(
        connection: psycopg.Connection[Any], event_id: str
    ) -> tuple[UsageComponent, ...]:
        rows = connection.execute(
            """SELECT unit_name, quantity FROM router.accounting_usage_components
               WHERE event_id = %s ORDER BY unit_name""",
            (event_id,),
        ).fetchall()
        return tuple(UsageComponent(UsageUnit(row[0]), row[1]) for row in rows)

    @staticmethod
    def _correction_usage(
        connection: psycopg.Connection[Any], correction_id: str
    ) -> tuple[UsageDelta, ...]:
        rows = connection.execute(
            """SELECT unit_name, quantity_delta
               FROM router.accounting_correction_usage
               WHERE correction_id = %s ORDER BY unit_name""",
            (correction_id,),
        ).fetchall()
        return tuple(UsageDelta(UsageUnit(row[0]), row[1]) for row in rows)

    @staticmethod
    def _check_correction_usage(
        connection: psycopg.Connection[Any], correction: AccountingCorrection
    ) -> None:
        source = connection.execute(
            "SELECT occurred_at FROM router.accounting_facts WHERE event_id = %s FOR SHARE",
            (correction.source_event_id,),
        ).fetchone()
        if source is None or correction.occurred_at < source[0]:
            raise AccountingError(
                "An accounting correction must not predate its source event."
            )
        for component in correction.usage_delta:
            row = connection.execute(
                """
                SELECT COALESCE(original.quantity, 0)
                       + COALESCE(sum(delta.quantity_delta), 0)
                FROM router.accounting_facts AS fact
                LEFT JOIN router.accounting_usage_components AS original
                  ON original.event_id = fact.event_id
                 AND original.unit_name = %s
                LEFT JOIN router.accounting_corrections AS existing
                  ON existing.source_event_id = fact.event_id
                LEFT JOIN router.accounting_correction_usage AS delta
                  ON delta.correction_id = existing.correction_id
                 AND delta.unit_name = %s
                WHERE fact.event_id = %s
                GROUP BY original.quantity
                """,
                (
                    component.unit.value,
                    component.unit.value,
                    correction.source_event_id,
                ),
            ).fetchone()
            if row is None or row[0] + component.quantity < 0:
                raise AccountingError(
                    "An accounting usage correction must not make usage negative."
                )

    def _event_amount(
        self, connection: psycopg.Connection[Any], event: AccountingEvent
    ) -> Decimal:
        if event.price_version_id is None:
            if event.reported_amount is None:
                raise AccountingError("Unpriced usage needs one reported amount.")
            return event.reported_amount
        rows = connection.execute(
            """
            SELECT version.currency::text, component.unit_name,
                   component.unit_quantity, component.unit_price
            FROM router.route_price_versions AS version
            JOIN router.route_price_components AS component
              ON component.price_version_id = version.id
            WHERE version.id = %s
            """,
            (event.price_version_id,),
        ).fetchall()
        if not rows or any(row[0] != event.currency for row in rows):
            raise AccountingError("The price version currency does not match.")
        prices = {row[1]: (row[2], row[3]) for row in rows}
        amount = Decimal(0)
        for usage in event.usage:
            price = prices.get(usage.unit.value)
            if price is None:
                raise AccountingError(
                    "The price version does not cover reported usage."
                )
            amount += usage.quantity / price[0] * price[1]
        try:
            return exact_decimal(amount)
        except ValueError as error:
            raise AccountingError(
                "The exact price calculation exceeds the accounting scale."
            ) from error

    @staticmethod
    def _price_routes(
        connection: psycopg.Connection[Any],
        service_id: str | None,
        source_name: str,
        route_ids: Sequence[str],
    ) -> list[tuple[Any, ...]]:
        if len(route_ids) > 10_000 or len(set(route_ids)) != len(route_ids):
            raise AccountingError("The price synchronization selection is invalid.")
        rows = connection.execute(
            """
            SELECT route.id::text,
                   COALESCE(source.lookup_identifier, route.provider_lookup_id),
                   source.authority_kind,
                   route.owner_kind, route.owner_service_id::text
            FROM router.provider_model_routes AS route
            JOIN router.route_price_sources AS source
              ON source.provider_model_route_id = route.id
            WHERE (
                    source.source_name = %s
                    OR (source.authority_kind = 'manual'
                        AND route.id = ANY(%s::uuid[]))
                  )
              AND (%s::uuid[] = '{}'::uuid[] OR route.id = ANY(%s::uuid[]))
            ORDER BY route.id
            """,
            (
                source_name,
                list(route_ids),
                list(route_ids),
                list(route_ids),
            ),
        ).fetchall()
        for row in rows:
            if service_id is not None and not (
                row[3] == "service" and row[4] == service_id
            ):
                raise AccountingError(
                    "A selected price route is outside the service scope."
                )
        if route_ids and len(rows) != len(route_ids):
            raise AccountingError("A selected price route is not available.")
        return rows

    def _normalize_price_row(
        self,
        connection: psycopg.Connection[Any],
        route: tuple[Any, ...],
        snapshot: SourceSnapshot,
        now: datetime,
    ) -> SynchronizationRow:
        route_id, lookup, authority, _, _ = route
        old = self._current_prices(connection, route_id)
        if authority == "manual":
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.SKIPPED,
                SynchronizationState.MANUAL,
                now,
            )
        if not snapshot.source_available:
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.FAILED,
                SynchronizationState.STALE if old else SynchronizationState.FAILED,
                now,
                error_class="source_unavailable",
            )
        candidate = snapshot.rows.get(lookup)
        if candidate is None:
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.MISSING,
                SynchronizationState.STALE if old else SynchronizationState.MISSING,
                now,
                error_class="missing_row",
            )
        if any(
            (item.unit.value if isinstance(item, PriceComponent) else item.unit)
            not in {unit.value for unit in UsageUnit}
            for item in candidate
        ):
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.FAILED,
                SynchronizationState.STALE if old else SynchronizationState.FAILED,
                now,
                error_class="unsupported_unit",
            )
        try:
            normalized = tuple(self._normalize_component(item) for item in candidate)
        except (TypeError, ValueError):
            normalized = ()
        if not normalized or len({item.unit for item in normalized}) != len(normalized):
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.FAILED,
                SynchronizationState.STALE if old else SynchronizationState.FAILED,
                now,
                error_class="invalid_value",
            )
        currencies = {item.currency for item in normalized}
        if len(currencies) != 1 or not self._route_currency_allowed(
            connection, route_id, next(iter(currencies))
        ):
            return SynchronizationRow(
                route_id,
                snapshot.source_name,
                lookup,
                old,
                old,
                SynchronizationStatus.FAILED,
                SynchronizationState.STALE if old else SynchronizationState.FAILED,
                now,
                error_class="currency_mismatch",
            )
        ordered = tuple(sorted(normalized, key=lambda item: item.unit.value))
        status = (
            SynchronizationStatus.UNCHANGED
            if ordered == old
            else SynchronizationStatus.UPDATED
        )
        return SynchronizationRow(
            route_id,
            snapshot.source_name,
            lookup,
            old,
            ordered,
            status,
            SynchronizationState.CURRENT,
            now,
            price_version_id=None
            if status is SynchronizationStatus.UNCHANGED
            else str(uuid.uuid4()),
        )

    @staticmethod
    def _normalize_component(
        item: PriceComponent | RawPriceComponent,
    ) -> PriceComponent:
        return (
            item
            if isinstance(item, PriceComponent)
            else PriceComponent(
                UsageUnit(item.unit),
                exact_decimal(item.price),
                item.currency,
                item.raw_source_value,
                exact_decimal(item.unit_quantity),
            )
        )

    @staticmethod
    def _route_currency_allowed(
        connection: psycopg.Connection[Any], route_id: str, currency: str
    ) -> bool:
        rows = connection.execute(
            """
            WITH RECURSIVE route_policy AS (
                SELECT owner_kind, owner_service_id, eligible_service_ids
                FROM router.provider_model_routes WHERE id = %s
            ), roots AS (
                SELECT service.id
                FROM router.services AS service, route_policy AS route
                WHERE service.state <> 'retired'
                  AND (
                    (route.owner_kind = 'global'
                     AND (route.eligible_service_ids = '{}'::uuid[]
                          OR service.id = ANY(route.eligible_service_ids)))
                    OR (route.owner_kind = 'service'
                        AND ((route.eligible_service_ids = '{}'::uuid[]
                              AND service.id = route.owner_service_id)
                             OR service.id = ANY(route.eligible_service_ids)))
                  )
            ), permitted_services AS (
                SELECT id FROM roots
              UNION
                SELECT service.id
                FROM router.services AS service
                JOIN permitted_services AS parent
                  ON service.parent_service_id = parent.id
                WHERE service.state <> 'retired'
            ), applicable AS (
                SELECT budget.currency
                FROM router.budget_scopes AS budget
                WHERE budget.scope_kind = 'global'
              UNION
                SELECT budget.currency
                FROM router.budget_scopes AS budget
                WHERE budget.scope_kind IN ('service', 'workspace')
                  AND (budget.service_id =
                       (SELECT owner_service_id FROM route_policy)
                       OR budget.service_id IN (SELECT id FROM permitted_services))
              UNION
                SELECT budget.currency
                FROM router.assignment_candidates AS candidate
                JOIN router.assignment_definitions AS assignment
                  ON assignment.id = candidate.assignment_id
                 AND assignment.configuration_revision_id =
                     candidate.configuration_revision_id
                JOIN router.active_configurations AS active
                  ON active.revision_id = assignment.configuration_revision_id
                JOIN router.budget_scopes AS budget
                  ON budget.assignment_id = candidate.assignment_id
                WHERE candidate.provider_model_route_id = %s
            )
            SELECT DISTINCT currency::text FROM applicable
            """,
            (route_id, route_id),
        ).fetchall()
        return not rows or all(row[0] == currency for row in rows)

    @staticmethod
    def _current_prices(
        connection: psycopg.Connection[Any], route_id: str
    ) -> tuple[PriceComponent, ...]:
        rows = connection.execute(
            """
            SELECT component.unit_name, component.unit_price,
                   version.currency::text, component.raw_source_value,
                   component.unit_quantity
            FROM router.route_price_versions AS version
            JOIN router.route_price_components AS component
              ON component.price_version_id = version.id
            WHERE version.provider_model_route_id = %s
              AND version.version_number = (
                  SELECT max(current.version_number)
                  FROM router.route_price_versions AS current
                  WHERE current.provider_model_route_id = %s
              )
            ORDER BY component.unit_name
            """,
            (route_id, route_id),
        ).fetchall()
        return tuple(
            PriceComponent(UsageUnit(row[0]), row[1], row[2], row[3], row[4])
            for row in rows
        )

    @staticmethod
    def _insert_price_version(
        connection: psycopg.Connection[Any],
        row: SynchronizationRow,
        snapshot_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"price-version:{row.provider_model_route_id}",),
        )
        version_number = connection.execute(
            """SELECT COALESCE(max(version_number), 0) + 1
               FROM router.route_price_versions WHERE provider_model_route_id = %s""",
            (row.provider_model_route_id,),
        ).fetchone()
        if version_number is None or row.price_version_id is None:
            raise AccountingError("The new price version identity is invalid.")
        connection.execute(
            """
            INSERT INTO router.route_price_versions (
                id, provider_model_route_id, source_snapshot_id, version_number,
                currency, status, accepted_at
            ) VALUES (%s, %s, %s, %s, %s, 'current', %s)
            """,
            (
                row.price_version_id,
                row.provider_model_route_id,
                snapshot_id,
                version_number[0],
                row.new_prices[0].currency,
                now,
            ),
        )
        for component in row.new_prices:
            connection.execute(
                """
                INSERT INTO router.route_price_components (
                    price_version_id, component_kind, unit_name, unit_quantity,
                    unit_price, raw_source_value
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    row.price_version_id,
                    component.unit.value,
                    component.unit.value,
                    component.unit_quantity,
                    component.price,
                    component.raw_source_value,
                ),
            )

    @staticmethod
    def _persist_synchronization_state(
        connection: psycopg.Connection[Any], row: SynchronizationRow
    ) -> None:
        connection.execute(
            """
            INSERT INTO router.route_price_synchronization_states (
                provider_model_route_id, synchronization_state,
                last_price_version_id, last_error_class, observed_at
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (provider_model_route_id) DO UPDATE
            SET synchronization_state = EXCLUDED.synchronization_state,
                last_price_version_id = COALESCE(
                    EXCLUDED.last_price_version_id,
                    router.route_price_synchronization_states.last_price_version_id
                ),
                last_error_class = EXCLUDED.last_error_class,
                observed_at = EXCLUDED.observed_at
            WHERE EXCLUDED.observed_at >=
                  router.route_price_synchronization_states.observed_at
            """,
            (
                row.provider_model_route_id,
                row.synchronization_state.value,
                row.price_version_id,
                row.error_class,
                row.synchronized_at,
            ),
        )

    @staticmethod
    def _publish_price_revision(
        connection: psycopg.Connection[Any],
        service_id: str | None,
        context: RequestContext,
        _operation_id: str,
        now: datetime,
    ) -> str:
        revision_id = str(uuid.uuid4())
        scope_kind = "service" if service_id else "global"
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"price-revision:{service_id or 'global'}",),
        )
        active = connection.execute(
            """
            SELECT active.revision_number, revision.content,
                   active.revision_id::text
            FROM router.active_configurations AS active
            JOIN router.configuration_revisions AS revision
              ON revision.id = active.revision_id
            WHERE active.scope_kind = %s
              AND active.service_id IS NOT DISTINCT FROM %s
              AND active.workspace_id IS NULL
            FOR UPDATE OF active
            """,
            (scope_kind, service_id),
        ).fetchone()
        if active is None:
            raise AccountingError(
                "A price synchronization needs an active configuration."
            )
        number = int(active[0]) + 1
        revision_content = active[1]
        changed_prices = {
            str(row[0]): (str(row[1]), row[2], str(row[3]))
            for row in connection.execute(
                """
                SELECT result.provider_model_route_id, result.price_version_id,
                       result.new_prices, result.synchronization_state
                FROM router.price_synchronization_results AS result
                JOIN router.provider_model_routes AS route
                  ON route.id = result.provider_model_route_id
                WHERE result.run_id = %s AND result.status = 'updated'
                  AND route.owner_kind = %s
                  AND route.owner_service_id IS NOT DISTINCT FROM %s
                """,
                (
                    _operation_id,
                    "service" if service_id else "global",
                    service_id,
                ),
            ).fetchall()
        }
        for route in revision_content.get("provider_model_routes", []):
            changed = changed_prices.get(route.get("provider_model_route_id"))
            if changed is None:
                continue
            route["price_version"] = changed[0]
            route["prices"] = changed[1]
            route["synchronization_state"] = changed[2]
        content = json.dumps(
            revision_content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(content.encode()).digest()
        connection.execute(
            """
            INSERT INTO router.configuration_revisions (
                id, scope_kind, service_id, revision_number, content,
                content_sha256, created_by_kind, created_by_id, created_at
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """,
            (
                revision_id,
                scope_kind,
                service_id,
                number,
                content,
                digest,
                "administrator"
                if context.actor_kind is PrincipalKind.ADMINISTRATOR
                else "system"
                if context.actor_kind is PrincipalKind.SYSTEM
                else "service",
                context.actor_id,
                now,
            ),
        )
        assignments = connection.execute(
            """SELECT id::text, stable_name, state, created_at
               FROM router.assignment_definitions
               WHERE configuration_revision_id = %s ORDER BY stable_name""",
            (active[2],),
        ).fetchall()
        for old_assignment_id, stable_name, state, created_at in assignments:
            new_assignment_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO router.assignment_definitions (
                       id, configuration_revision_id, stable_name, state,
                       created_at
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (new_assignment_id, revision_id, stable_name, state, created_at),
            )
            connection.execute(
                """INSERT INTO router.assignment_candidates (
                       assignment_id, configuration_revision_id, ordinal,
                       provider_model_route_id, attempt_timeout_seconds,
                       attempt_timeout_ms, candidate_policy
                   )
                   SELECT %s, %s, ordinal, provider_model_route_id,
                          attempt_timeout_seconds, attempt_timeout_ms,
                          candidate_policy
                   FROM router.assignment_candidates
                   WHERE assignment_id = %s ORDER BY ordinal""",
                (new_assignment_id, revision_id, old_assignment_id),
            )
        connection.execute(
            """INSERT INTO router.configuration_price_bindings (
                   configuration_revision_id, provider_model_route_id,
                   price_version_id
               )
               SELECT %s, prior.provider_model_route_id, prior.price_version_id
               FROM router.configuration_price_bindings AS prior
               WHERE prior.configuration_revision_id = %s
                 AND NOT EXISTS (
                     SELECT 1 FROM router.price_synchronization_results AS changed
                     WHERE changed.run_id = %s
                       AND changed.status = 'updated'
                       AND changed.provider_model_route_id = prior.provider_model_route_id
                 )""",
            (revision_id, active[2], _operation_id),
        )
        connection.execute(
            """INSERT INTO router.configuration_price_bindings (
                   configuration_revision_id, provider_model_route_id,
                   price_version_id
               )
               SELECT %s, result.provider_model_route_id, result.price_version_id
               FROM router.price_synchronization_results AS result
               JOIN router.provider_model_routes AS route
                 ON route.id = result.provider_model_route_id
               WHERE result.run_id = %s AND result.status = 'updated'
                 AND route.owner_kind = %s
                 AND route.owner_service_id IS NOT DISTINCT FROM %s""",
            (
                revision_id,
                _operation_id,
                "service" if service_id else "global",
                service_id,
            ),
        )
        connection.execute(
            """
            UPDATE router.active_configurations
            SET revision_id = %s, revision_number = %s, activated_at = %s
            WHERE scope_kind = %s
              AND service_id IS NOT DISTINCT FROM %s
              AND workspace_id IS NULL
            """,
            (revision_id, number, now, scope_kind, service_id),
        )
        connection.execute(
            """
            INSERT INTO router.configuration_distribution_states (
                revision_id, published_at, observed_at
            ) VALUES (%s, %s, %s)
            """,
            (revision_id, now, now),
        )
        publication_audit_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO router.audit_events (
                   event_id, audit_class, actor_kind, actor_id, authority_class,
                   service_id, action, permission_result, safe_details, occurred_at
               ) VALUES (%s, %s, %s, %s, %s, %s, 'price.publish',
                         'permitted', %s::jsonb, %s)""",
            (
                publication_audit_id,
                "global_administration"
                if context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
                else "security",
                "administrator"
                if context.actor_kind is PrincipalKind.ADMINISTRATOR
                else "system"
                if context.actor_kind is PrincipalKind.SYSTEM
                else "service",
                context.actor_id,
                context.authority_class.value,
                service_id,
                json.dumps({"synchronization_run_id": _operation_id}),
                now,
            ),
        )
        connection.execute(
            """INSERT INTO router.configuration_audit_bindings (
                   revision_id, event_id
               ) VALUES (%s, %s)""",
            (revision_id, publication_audit_id),
        )
        return revision_id

    @staticmethod
    def _insert_sync_audit(
        connection: psycopg.Connection[Any],
        context: RequestContext,
        service_id: str | None,
        operation_id: str,
        rows: Sequence[SynchronizationRow],
        dry_run: bool,
        now: datetime,
    ) -> None:
        counts = {
            status.value: sum(row.status is status for row in rows)
            for status in SynchronizationStatus
        }
        details = json.dumps(
            {"dry_run": dry_run, "result_counts": counts},
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """INSERT INTO router.audit_events (
                   event_id, audit_class, actor_kind, actor_id, authority_class,
                   service_id, action, permission_result, safe_details, occurred_at
               ) VALUES (%s, %s, %s, %s, %s, %s, 'price.synchronize',
                         'permitted', %s::jsonb, %s)""",
            (
                operation_id,
                "global_administration"
                if context.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
                else "security",
                "administrator"
                if context.actor_kind is PrincipalKind.ADMINISTRATOR
                else "service",
                context.actor_id,
                context.authority_class.value,
                service_id,
                details,
                now,
            ),
        )

    @staticmethod
    def _price_json(prices: tuple[PriceComponent, ...]) -> str:
        return json.dumps(
            [
                {
                    "unit": item.unit.value,
                    "price": str(item.price),
                    "currency": item.currency,
                    "raw_source_value": item.raw_source_value,
                    "unit_quantity": str(item.unit_quantity),
                }
                for item in prices
            ],
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _synchronization_result(
        connection: psycopg.Connection[Any], operation_id: str
    ) -> SynchronizationResult:
        run = connection.execute(
            """SELECT run.dry_run, run.source_snapshot_id::text,
                      run.resulting_configuration_revision_id::text, run.state,
                      snapshot.source_name, snapshot.fetched_at,
                      encode(snapshot.content_sha256, 'hex'),
                      snapshot.source_revision, snapshot.http_validator
               FROM router.price_synchronization_runs AS run
               JOIN router.price_source_snapshots AS snapshot
                 ON snapshot.id = run.source_snapshot_id
               WHERE run.id = %s""",
            (operation_id,),
        ).fetchone()
        if run is None:
            raise AccountingError("The price synchronization does not exist.")
        rows = connection.execute(
            """SELECT result.provider_model_route_id::text, run.source_name,
                      result.lookup_identifier, result.old_prices,
                      result.new_prices, result.status, result.synchronization_state,
                      result.synchronized_at, result.price_version_id::text,
                      result.error_class
               FROM router.price_synchronization_results AS result
               JOIN router.price_synchronization_runs AS run ON run.id = result.run_id
               WHERE result.run_id = %s ORDER BY result.provider_model_route_id""",
            (operation_id,),
        ).fetchall()

        def prices(value: list[dict[str, str]]) -> tuple[PriceComponent, ...]:
            return tuple(
                PriceComponent(
                    UsageUnit(item["unit"]),
                    Decimal(item["price"]),
                    item["currency"],
                    item["raw_source_value"],
                    Decimal(item.get("unit_quantity", "1")),
                )
                for item in value
            )

        publication_rows = connection.execute(
            """SELECT configuration_revision_id::text
               FROM router.price_synchronization_publications
               WHERE synchronization_run_id = %s
               ORDER BY service_id NULLS FIRST""",
            (operation_id,),
        ).fetchall()
        revisions = tuple(row[0] for row in publication_rows)
        return SynchronizationResult(
            operation_id,
            bool(run[0]),
            str(run[1]),
            tuple(
                SynchronizationRow(
                    row[0],
                    row[1],
                    row[2],
                    prices(row[3]),
                    prices(row[4]),
                    SynchronizationStatus(row[5]),
                    SynchronizationState(row[6]),
                    row[7],
                    row[8],
                    row[9],
                )
                for row in rows
            ),
            run[2],
            revisions,
            SynchronizationRunState(run[3]),
            SourceSnapshotEvidence(run[4], run[5], run[6], run[7], run[8]),
        )

    @staticmethod
    def _require_aware_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise AccountingError("The operation time must include a time zone.")

    @classmethod
    def _require_time_range(cls, start: datetime, end: datetime) -> None:
        cls._require_aware_time(start)
        cls._require_aware_time(end)
        if start >= end:
            raise AccountingError("The operation time range is invalid.")

    @staticmethod
    def _scope_filter(scope: Scope) -> tuple[str, tuple[str, ...]]:
        if scope.service_id is None:
            raise AccountingError("An accounting summary must be service scoped.")
        if scope.workspace_id is None:
            return "service_id = %s", (scope.service_id,)
        return (
            "service_id = %s AND workspace_id = %s",
            (scope.service_id, scope.workspace_id),
        )

    @staticmethod
    def _scope_currency(connection: psycopg.Connection[Any], scope: Scope) -> str:
        row = connection.execute(
            """
            SELECT currency::text FROM router.budget_scopes
            WHERE service_id = %s AND workspace_id IS NOT DISTINCT FROM %s
            ORDER BY CASE scope_kind WHEN 'workspace' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (scope.service_id, scope.workspace_id),
        ).fetchone()
        if row is None:
            raise AccountingError("The accounting scope has no configured currency.")
        return str(row[0])

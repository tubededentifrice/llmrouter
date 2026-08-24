"""Exact price synchronization, durable accounting, and bounded statistics."""
# ruff: noqa: C901, D105, D107, E501, EM101, PLR0912, PLR0913, PLR0915, PLR2004, TRY003, TRY004, TRY301

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import httpx
from opendle import RouterContractError, normalize_tags
from psycopg import sql
from psycopg.types.json import Jsonb

from llmrouter_backend.errors import conflict, invalid_request
from llmrouter_backend.models import (
    Price,
    PriceSyncItem,
    PriceSyncResult,
    StatisticsBucket,
    StatisticsDimension,
    StatisticsResult,
    UnitPriceWrite,
    UsageItem,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from psycopg import Connection

_CATALOG_WRITE_LOCK = 4_993_044_345_823
_PRICE_SYNCHRONIZATION_LOCK = 4_993_044_345_825
_ROLLUP_LOCK_NAMESPACE = 4_993_044
_MAXIMUM_DECIMAL = Decimal("99999999999999999999.999999999999999999")
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_MAXIMUM_SOURCE_BYTES = 10_000_000
_SOURCE_OPERATION_TIMEOUT_SECONDS = 10.0
_SOURCE_TOTAL_TIMEOUT_SECONDS = 30.0
_DIMENSION_SQL = {
    "date": "to_char((attempt.started_at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD')",
    "service": "service.api_name::text",
    "workspace": "workspace.api_name::text",
    "assignment": "COALESCE(call.assignment_api_name::text, '(exact)')",
    "provider_model": "attempt.provider_model_api_name::text",
    "outcome": "attempt.outcome",
    "tag": "selected_tag.value",
}
_PRICE_TARGETS_SQL = """SELECT mapping.api_name AS provider_model_api_name,
          CASE WHEN mapping.price_source IS NOT NULL
               THEN 'provider_model' ELSE 'model' END AS owner_kind,
          CASE WHEN mapping.price_source IS NOT NULL
               THEN mapping.id ELSE model.id END AS owner_id,
          CASE WHEN mapping.manual_price IS NOT NULL THEN NULL
               ELSE COALESCE(mapping.price_source, model.price_source)
               END AS source_name,
          CASE WHEN mapping.manual_price IS NOT NULL THEN NULL
               ELSE COALESCE(mapping.price_lookup_key, model.price_lookup_key)
               END AS lookup_key,
          CASE WHEN mapping.price_source IS NOT NULL THEN mapping.synchronized_price
               WHEN mapping.manual_price IS NULL AND model.price_source IS NOT NULL
               THEN model.synchronized_price
               ELSE NULL END AS current_price
   FROM router.provider_models AS mapping
   JOIN router.canonical_models AS model ON model.id = mapping.model_id
   WHERE ((%s::text[] = '{}'::text[]
           AND (mapping.price_source IS NOT NULL
                OR (mapping.manual_price IS NULL
                    AND model.price_source IS NOT NULL)))
          OR mapping.api_name = ANY(%s::text[])
          OR (
              mapping.price_source IS NULL
              AND mapping.manual_price IS NULL
              AND model.price_source IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM router.provider_models AS selected
                  WHERE selected.api_name = ANY(%s::text[])
                    AND selected.model_id = mapping.model_id
                    AND selected.price_source IS NULL
                    AND selected.manual_price IS NULL
              )
          ))
   ORDER BY mapping.api_name"""


class PriceSource(Protocol):
    """Fetch one bounded immutable source snapshot."""

    def fetch(self) -> Mapping[str, Price]:
        """Return source lookup keys and their current typed prices."""


class UnavailablePriceSource:
    """Keep a registered source safe until a deployment supplies its client."""

    def fetch(self) -> Mapping[str, Price]:
        """Report dependency unavailability without a mutable partial result."""
        raise RuntimeError("The registered price source is unavailable.")


class OpenRouterPriceSource:
    """Read the public OpenRouter model catalog as one source snapshot."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._transport = transport
        self._monotonic_clock = monotonic_clock

    def fetch(self) -> Mapping[str, Price]:
        """Fetch and normalize supported OpenRouter price units once."""
        deadline = self._monotonic_clock() + _SOURCE_TOTAL_TIMEOUT_SECONDS

        def remaining_seconds() -> float:
            remaining = deadline - self._monotonic_clock()
            if remaining <= 0:
                raise TimeoutError(
                    "The OpenRouter catalog fetch exceeded its deadline."
                )
            return remaining

        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(_SOURCE_OPERATION_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            remaining_seconds()
            with client.stream("GET", _OPENROUTER_MODELS_URL) as response:
                response.raise_for_status()
                remaining_seconds()
                content = bytearray()
                chunks = iter(response.iter_bytes())
                while True:
                    read_timeout = min(
                        _SOURCE_OPERATION_TIMEOUT_SECONDS, remaining_seconds()
                    )
                    timeout_extension = response.request.extensions.get("timeout")
                    if isinstance(timeout_extension, dict):
                        timeout_extension["read"] = read_timeout
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    remaining_seconds()
                    if len(chunk) > _MAXIMUM_SOURCE_BYTES - len(content):
                        raise ValueError("The OpenRouter catalog is too large.")
                    content.extend(chunk)
                remaining_seconds()
            value = json.loads(content)
            remaining_seconds()
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise ValueError("The OpenRouter catalog shape is invalid.")
        if len(value["data"]) > 10_000:
            raise ValueError("The OpenRouter catalog is too large.")
        result: dict[str, Price] = {}
        field_units = {
            "prompt": "input_token",
            "completion": "output_token",
            "input_cache_read": "cached_input_token",
            "image": "image",
            "request": "request",
        }
        for row in value["data"]:
            remaining_seconds()
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            key = row["id"]
            pricing = row.get("pricing")
            if not 1 <= len(key) <= 500 or not isinstance(pricing, dict):
                continue
            units: list[UnitPriceWrite] = []
            for field, unit in field_units.items():
                amount = pricing.get(field)
                if not isinstance(amount, str):
                    continue
                try:
                    parsed = _exact_decimal(amount, positive=True)
                except ValueError:
                    continue
                units.append(
                    UnitPriceWrite(unit=cast("Any", unit), amount=_decimal_text(parsed))
                )
            if units:
                result[key] = Price(currency="USD", unit_prices=units)
        remaining_seconds()
        return result


@dataclass(frozen=True, slots=True)
class UsageAmount:
    """One immutable provider-reported typed usage value."""

    unit: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.unit not in {
            "input_token",
            "output_token",
            "cached_input_token",
            "image",
            "video_second",
            "audio_second",
            "request",
            "provider_unit",
        }:
            raise ValueError("The usage unit is not supported.")
        object.__setattr__(self, "quantity", _exact_decimal(self.quantity))


@dataclass(frozen=True, slots=True)
class PriceRate:
    """One immutable applied unit price."""

    unit: str
    amount: Decimal

    def __post_init__(self) -> None:
        UsageAmount(self.unit, Decimal(0))
        object.__setattr__(self, "amount", _exact_decimal(self.amount))


@dataclass(frozen=True, slots=True)
class AttemptPriceSnapshot:
    """One immutable typed price selected before a provider attempt."""

    currency: str
    unit_prices: tuple[PriceRate, ...]
    source: str | None = None
    synchronized_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
        ):
            raise ValueError("The accounting currency is invalid.")
        units = [item.unit for item in self.unit_prices]
        if not units or len(units) > 16 or len(units) != len(set(units)):
            raise ValueError("The applied price units are invalid.")
        if self.source is not None and not 1 <= len(self.source) <= 500:
            raise ValueError("The applied price source is invalid.")
        if self.synchronized_at is not None:
            _require_aware(self.synchronized_at)


@dataclass(frozen=True, slots=True)
class AttemptAccountingWrite:
    """One immutable provider-attempt accounting fact."""

    id: uuid.UUID
    provider_connection_api_name: str
    provider_model_api_name: str
    outcome: str
    usage: tuple[UsageAmount, ...]
    applied_price: AttemptPriceSnapshot
    started_at: datetime
    completed_at: datetime
    failure_class: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "failed"}:
            raise ValueError("The attempt outcome is invalid.")
        units = [item.unit for item in self.usage]
        if len(units) != len(set(units)):
            raise ValueError("A usage unit cannot occur more than once.")
        _require_interval(self.started_at, self.completed_at)
        if self.failure_class is not None and self.failure_class not in {
            "authentication",
            "rate_limited",
            "timeout",
            "transport",
            "unavailable",
            "refusal",
            "incompatible",
            "invalid_response",
            "interrupted",
            "upstream_failed",
        }:
            raise ValueError("The safe failure class is invalid.")
        if (self.outcome == "succeeded") != (self.failure_class is None):
            raise ValueError("The attempt outcome and failure class do not agree.")


@dataclass(frozen=True, slots=True)
class CallAccountingWrite:
    """One logical call and all provider attempts that it owns."""

    id: uuid.UUID
    service_id: uuid.UUID
    workspace_id: uuid.UUID
    assignment_api_name: str | None
    tags: tuple[str, ...]
    outcome: str
    started_at: datetime
    completed_at: datetime
    attempts: tuple[AttemptAccountingWrite, ...]

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "failed"}:
            raise ValueError("The call outcome is invalid.")
        _require_interval(self.started_at, self.completed_at)
        if len(self.attempts) > 16:
            raise ValueError("A call can contain no more than 16 attempts.")
        if len({item.id for item in self.attempts}) != len(self.attempts):
            raise ValueError("An attempt identity cannot occur more than once.")
        if any(
            item.started_at < self.started_at or item.completed_at > self.completed_at
            for item in self.attempts
        ):
            raise ValueError("An attempt must stay inside its logical call interval.")
        if any(
            later.started_at < earlier.completed_at
            for earlier, later in zip(self.attempts, self.attempts[1:], strict=False)
        ):
            raise ValueError("Provider attempts must use their recorded order.")
        succeeded = [item for item in self.attempts if item.outcome == "succeeded"]
        if self.outcome == "succeeded" and not self.attempts:
            raise ValueError("A successful call must contain one successful attempt.")
        if (self.outcome == "succeeded" and succeeded != [self.attempts[-1]]) or (
            self.outcome == "failed" and succeeded
        ):
            raise ValueError("The logical call and attempt outcomes do not agree.")
        try:
            normalized = normalize_tags(self.tags)
        except RouterContractError as error:
            raise ValueError("The accounting tags are invalid.") from error
        object.__setattr__(self, "tags", normalized)


def default_price_sources(
    openrouter_transport: httpx.BaseTransport | None = None,
) -> dict[str, PriceSource]:
    """Return the fixed registered source set."""
    return {
        "openrouter": OpenRouterPriceSource(openrouter_transport),
        "wavespeed": UnavailablePriceSource(),
    }


def effective_price_snapshot(
    connection: Connection[Any], provider_model_api_name: str
) -> AttemptPriceSnapshot | None:
    """Read one effective manual or synchronized price for a later attempt."""
    row = connection.execute(
        """SELECT CASE
                     WHEN mapping.price_source IS NOT NULL THEN mapping.synchronized_price
                     WHEN mapping.manual_price IS NOT NULL THEN mapping.manual_price
                     WHEN model.price_source IS NOT NULL THEN model.synchronized_price
                     ELSE model.manual_price END AS price
           FROM router.provider_models AS mapping
           JOIN router.canonical_models AS model ON model.id = mapping.model_id
           JOIN router.provider_connections AS provider ON provider.id = mapping.provider_id
           WHERE mapping.api_name = %s AND mapping.enabled AND provider.enabled""",
        (provider_model_api_name,),
    ).fetchone()
    if row is None or row["price"] is None:
        return None
    return _price_snapshot(Price.model_validate(row["price"]))


def synchronize_prices(
    connection: Connection[Any],
    *,
    sources: Mapping[str, PriceSource],
    provider_model_api_names: Sequence[str] | None = None,
    now: datetime | None = None,
    run_kind: Literal["scheduled", "on_demand"] = "on_demand",
) -> tuple[uuid.UUID, PriceSyncResult]:
    """Fetch each selected source once and atomically apply all valid rows."""
    with connection.transaction():
        lock = connection.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
            (_PRICE_SYNCHRONIZATION_LOCK,),
        ).fetchone()
        if lock is None or not lock["acquired"]:
            raise conflict("A price synchronization is already in progress.")
        return _synchronize_prices(
            connection,
            sources=sources,
            provider_model_api_names=provider_model_api_names,
            now=now,
            run_kind=run_kind,
        )


def _synchronize_prices(
    connection: Connection[Any],
    *,
    sources: Mapping[str, PriceSource],
    provider_model_api_names: Sequence[str] | None,
    now: datetime | None,
    run_kind: Literal["scheduled", "on_demand"],
) -> tuple[uuid.UUID, PriceSyncResult]:
    attempted_at = now or datetime.now(tz=UTC)
    _require_aware(attempted_at)
    names = tuple(provider_model_api_names or ())
    if len(names) > 1000 or len(names) != len(set(names)):
        raise invalid_request(
            "provider_model_api_names", "The synchronization selection is invalid."
        )
    initial_rows = connection.execute(
        _PRICE_TARGETS_SQL,
        (list(names), list(names), list(names)),
    ).fetchall()
    if names:
        found = {row["provider_model_api_name"] for row in initial_rows}
        if not set(names) <= found:
            raise invalid_request(
                "provider_model_api_names", "A selected provider-model does not exist."
            )
    source_names = sorted(
        {
            cast("str", row["source_name"])
            for row in initial_rows
            if row["source_name"] is not None
        }
    )
    snapshots: dict[str, Mapping[str, Price] | Exception] = {}
    for source_name in source_names:
        source = sources.get(source_name)
        if source is None:
            snapshots[source_name] = RuntimeError("The source is not registered.")
            continue
        try:
            fetched_snapshot = source.fetch()
            if len(fetched_snapshot) > 10_000:
                raise ValueError("The source snapshot is too large.")
            snapshots[source_name] = fetched_snapshot
        except Exception as error:  # noqa: BLE001 - One source failure is a row result.
            snapshots[source_name] = error

    # Do not hold the catalog lock during network work. Re-read targets after
    # the lock so the update uses one committed configuration state.
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_CATALOG_WRITE_LOCK,))
    rows = connection.execute(
        _PRICE_TARGETS_SQL,
        (list(names), list(names), list(names)),
    ).fetchall()
    if names and not set(names) <= {row["provider_model_api_name"] for row in rows}:
        raise invalid_request(
            "provider_model_api_names", "A selected provider-model does not exist."
        )

    items: list[PriceSyncItem] = []
    owner_updates: dict[tuple[str, uuid.UUID], Price] = {}
    for row in rows:
        source_name = row["source_name"]
        lookup_key = row["lookup_key"]
        if source_name is None or lookup_key is None:
            items.append(
                PriceSyncItem(
                    provider_model_api_name=row["provider_model_api_name"],
                    outcome="failed",
                    message="The provider-model does not select a price source.",
                )
            )
            continue
        source_snapshot = snapshots.get(
            cast("str", source_name),
            RuntimeError("The price authority changed during synchronization."),
        )
        if isinstance(source_snapshot, Exception):
            items.append(
                PriceSyncItem(
                    provider_model_api_name=row["provider_model_api_name"],
                    outcome="failed",
                    message="The price source could not be read.",
                )
            )
            continue
        incoming = source_snapshot.get(cast("str", lookup_key))
        if incoming is None:
            items.append(
                PriceSyncItem(
                    provider_model_api_name=row["provider_model_api_name"],
                    outcome="missing",
                    message="The price source has no matching row.",
                )
            )
            continue
        try:
            accepted = _synchronized_price(
                incoming, cast("str", source_name), attempted_at
            )
        except Exception:  # noqa: BLE001 - One invalid source row must not stop peers.
            items.append(
                PriceSyncItem(
                    provider_model_api_name=row["provider_model_api_name"],
                    outcome="failed",
                    message="The price source row is invalid.",
                )
            )
            continue
        current = (
            Price.model_validate(row["current_price"]) if row["current_price"] else None
        )
        outcome = (
            "unchanged"
            if current and _price_values(current) == _price_values(accepted)
            else "updated"
        )
        items.append(
            PriceSyncItem(
                provider_model_api_name=row["provider_model_api_name"],
                outcome=cast("Any", outcome),
                price=accepted,
            )
        )
        owner_updates[(row["owner_kind"], row["owner_id"])] = accepted

    for (owner_kind, owner_id), price in owner_updates.items():
        table = (
            "provider_models" if owner_kind == "provider_model" else "canonical_models"
        )
        connection.execute(
            sql.SQL(
                "UPDATE router.{} SET synchronized_price = %s WHERE id = %s"
            ).format(sql.Identifier(table)),
            (Jsonb(price.model_dump(mode="json", exclude_none=True)), owner_id),
        )
    result = PriceSyncResult(attempted_at=attempted_at, items=items)
    synchronization_id = uuid.uuid4()
    completed = all(item.outcome in {"updated", "unchanged"} for item in items)
    connection.execute(
        """INSERT INTO router.price_synchronizations
               (id, attempted_at, run_kind, result, completed, failure_class)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            synchronization_id,
            attempted_at,
            run_kind,
            Jsonb([item.model_dump(mode="json", exclude_none=True) for item in items]),
            completed,
            None if completed else "source_failure",
        ),
    )
    return synchronization_id, result


def record_call_accounting(
    connection: Connection[Any], value: CallAccountingWrite
) -> None:
    """Store one logical call and immutable attempt snapshots in one transaction."""
    with connection.transaction():
        _record_call_accounting(connection, value)


def _record_call_accounting(
    connection: Connection[Any], value: CallAccountingWrite
) -> None:
    workspace = connection.execute(
        """SELECT 1 FROM router.workspaces
           WHERE service_id = %s AND id = %s FOR KEY SHARE""",
        (value.service_id, value.workspace_id),
    ).fetchone()
    if workspace is None:
        raise ValueError("The accounting workspace does not exist.")
    connection.execute(
        """INSERT INTO router.raw_accounting_calls
               (id, service_id, workspace_id, assignment_api_name, tags,
                outcome, started_at, completed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            value.id,
            value.service_id,
            value.workspace_id,
            value.assignment_api_name,
            list(value.tags),
            value.outcome,
            value.started_at,
            value.completed_at,
        ),
    )
    for position, attempt in enumerate(value.attempts):
        rates = {item.unit: item.amount for item in attempt.applied_price.unit_prices}
        missing = sorted({item.unit for item in attempt.usage} - rates.keys())
        if missing:
            raise ValueError("The applied price does not cover all reported usage.")
        cost = sum(
            (item.quantity * rates[item.unit] for item in attempt.usage),
            start=Decimal(0),
        )
        if abs(cost) > _MAXIMUM_DECIMAL * _MAXIMUM_DECIMAL:
            raise ValueError("The attempt cost is outside its safe range.")
        usage = [
            {"unit": item.unit, "quantity": _decimal_text(item.quantity)}
            for item in sorted(attempt.usage, key=lambda item: item.unit)
        ]
        price = _snapshot_json(attempt.applied_price)
        connection.execute(
            """INSERT INTO router.raw_accounting_attempts
                   (id, call_id, service_id, workspace_id, position,
                    provider_connection_api_name, provider_model_api_name,
                    outcome, usage, applied_price,
                    cost, currency, failure_class, started_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                attempt.id,
                value.id,
                value.service_id,
                value.workspace_id,
                position,
                attempt.provider_connection_api_name,
                attempt.provider_model_api_name,
                attempt.outcome,
                Jsonb(usage),
                Jsonb(price),
                cost,
                attempt.applied_price.currency,
                attempt.failure_class,
                attempt.started_at,
                attempt.completed_at,
            ),
        )


def rollup_day(
    connection: Connection[Any], day: date, *, now: datetime | None = None
) -> None:
    """Replace one closed UTC-day aggregate safely under a day lock."""
    with connection.transaction():
        _rollup_day(connection, day, now=now)


def _rollup_day(
    connection: Connection[Any], day: date, *, now: datetime | None
) -> None:
    current = now or datetime.now(tz=UTC)
    _require_aware(current)
    if day >= current.astimezone(UTC).date():
        raise ValueError("Only a closed UTC day can be rolled up.")
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (_ROLLUP_LOCK_NAMESPACE, day.toordinal()),
    )
    connection.execute("DELETE FROM router.daily_accounting WHERE day = %s", (day,))
    connection.execute(
        """WITH source_attempts AS MATERIALIZED (
               SELECT attempt.*, call.assignment_api_name, call.tags
               FROM router.raw_accounting_attempts AS attempt
               JOIN router.raw_accounting_calls AS call ON call.id = attempt.call_id
               WHERE attempt.started_at >= (%s::date::timestamp AT TIME ZONE 'UTC')
                 AND attempt.started_at <
                     ((%s::date + 1)::timestamp AT TIME ZONE 'UTC')
           ), inserted AS (
               INSERT INTO router.daily_accounting
                   (service_id, workspace_id, day, assignment_api_name,
                    provider_model_api_name, outcome, tags, usage_unit, currency,
                    calls, attempts, quantity, cost, rolled_up_at)
               SELECT attempt.service_id, attempt.workspace_id, %s,
                      attempt.assignment_api_name, attempt.provider_model_api_name,
                      attempt.outcome, attempt.tags, usage.unit, attempt.currency,
                      count(DISTINCT attempt.call_id), count(DISTINCT attempt.id),
                      COALESCE(sum(usage.quantity), 0),
                      COALESCE(sum(usage.quantity * price.amount), 0), %s
               FROM source_attempts AS attempt
               LEFT JOIN LATERAL jsonb_to_recordset(attempt.usage)
                   AS usage(unit text, quantity numeric) ON true
               LEFT JOIN LATERAL jsonb_to_recordset(
                   attempt.applied_price -> 'unit_prices'
               ) AS price(unit text, amount numeric) ON price.unit = usage.unit
               WHERE usage.unit IS NULL OR price.unit IS NOT NULL
               GROUP BY attempt.service_id, attempt.workspace_id,
                        attempt.assignment_api_name,
                        attempt.provider_model_api_name, attempt.outcome,
                        attempt.tags, usage.unit, attempt.currency
               RETURNING 1
           )
           INSERT INTO router.accounting_rollups
               (day, completed_at, attempt_count)
           SELECT %s, %s, count(*) FROM source_attempts
           ON CONFLICT (day) DO UPDATE
           SET completed_at = EXCLUDED.completed_at,
               attempt_count = EXCLUDED.attempt_count""",
        (day, day, day, current, day, current),
    )


def rollup_pending_days(
    connection: Connection[Any], *, now: datetime | None = None, limit: int = 32
) -> tuple[date, ...]:
    """Catch up a bounded set of closed days after a restart."""
    current = now or datetime.now(tz=UTC)
    _require_aware(current)
    if not 1 <= limit <= 366:
        raise ValueError("The rollup batch limit is invalid.")
    rows = connection.execute(
        """SELECT (attempt.started_at AT TIME ZONE 'UTC')::date AS day
           FROM router.raw_accounting_attempts AS attempt
           LEFT JOIN router.accounting_rollups AS rollup
             ON rollup.day = (attempt.started_at AT TIME ZONE 'UTC')::date
           WHERE attempt.started_at <
                 (%s::date::timestamp AT TIME ZONE 'UTC')
           GROUP BY (attempt.started_at AT TIME ZONE 'UTC')::date,
                    rollup.completed_at, rollup.attempt_count
           HAVING rollup.completed_at IS NULL
               OR count(*) <> rollup.attempt_count
               OR max(attempt.recorded_at) > rollup.completed_at
           ORDER BY day LIMIT %s""",
        (current.astimezone(UTC).date(), limit),
    ).fetchall()
    days = tuple(cast("date", row["day"]) for row in rows)
    for selected_day in days:
        rollup_day(connection, selected_day, now=current)
    return days


def statistics(
    connection: Connection[Any],
    *,
    from_time: datetime,
    to_time: datetime,
    group_by: Sequence[StatisticsDimension],
    service_id: uuid.UUID | None = None,
    service: str | None = None,
    workspace: str | None = None,
    assignment: str | None = None,
    provider_model: str | None = None,
    outcome: str | None = None,
    tag: str | None = None,
) -> StatisticsResult:
    """Read no more than 1000 exact groups from one bounded service scope."""
    _require_statistics_range(from_time, to_time)
    dimensions = tuple(group_by)
    if len(dimensions) > 8 or len(dimensions) != len(set(dimensions)):
        raise invalid_request(
            "group_by", "Statistics groups must be unique and bounded."
        )
    if outcome is not None and outcome not in {"succeeded", "failed"}:
        raise invalid_request("outcome", "The outcome filter is invalid.")
    if tag is not None:
        try:
            if normalize_tags((tag,)) != (tag,):
                raise RouterContractError("The tag is not canonical.")
        except RouterContractError as error:
            raise invalid_request("tag", "The tag filter is invalid.") from error
    expressions = [sql.SQL(_DIMENSION_SQL[item]) for item in dimensions]
    dimension_select = (
        sql.SQL(", ").join(expressions) if expressions else sql.SQL("NULL::text")
    )
    dimension_group = (
        sql.SQL(", ").join(expressions) if expressions else sql.SQL("NULL::text")
    )
    tag_join = (
        sql.SQL(
            """CROSS JOIN LATERAL unnest(
                   CASE WHEN cardinality(call.tags) = 0 THEN ARRAY['']::text[]
                        ELSE call.tags END
               ) AS selected_tag(value)"""
        )
        if "tag" in dimensions
        else sql.SQL(
            "LEFT JOIN LATERAL (SELECT NULL::text AS value) AS selected_tag ON true"
        )
    )
    query = sql.SQL(
        """WITH grouped AS (
           SELECT ARRAY[{dimension_select}]::text[] AS dimensions,
                    attempt.currency, count(DISTINCT call.id) AS calls,
                    count(DISTINCT attempt.id) AS attempts, sum(attempt.cost) AS cost
             FROM router.raw_accounting_attempts AS attempt
             JOIN router.raw_accounting_calls AS call ON call.id = attempt.call_id
             JOIN router.services AS service ON service.id = attempt.service_id
             JOIN router.workspaces AS workspace ON workspace.id = attempt.workspace_id
             {tag_join}
             WHERE attempt.started_at >= %s AND attempt.started_at < %s
               AND (%s::uuid IS NULL OR attempt.service_id = %s)
               AND (%s::text IS NULL OR service.api_name = %s)
               AND (%s::text IS NULL OR workspace.api_name = %s)
               AND (%s::text IS NULL OR
                    COALESCE(call.assignment_api_name::text, '(exact)') = %s)
               AND (%s::text IS NULL OR attempt.provider_model_api_name = %s)
               AND (%s::text IS NULL OR attempt.outcome = %s)
               AND (%s::text IS NULL OR %s = ANY(call.tags))
             GROUP BY {dimension_group}, attempt.currency
           ), unit_rows AS (
             SELECT ARRAY[{dimension_select}]::text[] AS dimensions,
                    attempt.currency, usage.unit, sum(usage.quantity) AS quantity
             FROM router.raw_accounting_attempts AS attempt
             JOIN router.raw_accounting_calls AS call ON call.id = attempt.call_id
             JOIN router.services AS service ON service.id = attempt.service_id
             JOIN router.workspaces AS workspace ON workspace.id = attempt.workspace_id
             {tag_join}
             CROSS JOIN LATERAL jsonb_to_recordset(attempt.usage)
                 AS usage(unit text, quantity numeric)
             WHERE attempt.started_at >= %s AND attempt.started_at < %s
               AND (%s::uuid IS NULL OR attempt.service_id = %s)
               AND (%s::text IS NULL OR service.api_name = %s)
               AND (%s::text IS NULL OR workspace.api_name = %s)
               AND (%s::text IS NULL OR
                    COALESCE(call.assignment_api_name::text, '(exact)') = %s)
               AND (%s::text IS NULL OR attempt.provider_model_api_name = %s)
               AND (%s::text IS NULL OR attempt.outcome = %s)
               AND (%s::text IS NULL OR %s = ANY(call.tags))
             GROUP BY {dimension_group}, attempt.currency, usage.unit
           )
           SELECT grouped.dimensions, grouped.currency, grouped.calls,
                  grouped.attempts, grouped.cost,
                  COALESCE(jsonb_agg(
                    jsonb_build_object('unit', unit_rows.unit,
                                       'quantity', unit_rows.quantity::text)
                    ORDER BY unit_rows.unit
                  ) FILTER (WHERE unit_rows.unit IS NOT NULL), '[]'::jsonb) AS units
           FROM grouped
           LEFT JOIN unit_rows USING (dimensions, currency)
           GROUP BY grouped.dimensions, grouped.currency, grouped.calls,
                    grouped.attempts, grouped.cost
           ORDER BY grouped.dimensions, grouped.currency LIMIT 1001"""
    ).format(
        dimension_select=dimension_select,
        dimension_group=dimension_group,
        tag_join=tag_join,
    )
    filters: tuple[Any, ...] = (
        from_time,
        to_time,
        service_id,
        service_id,
        service,
        service,
        workspace,
        workspace,
        assignment,
        assignment,
        provider_model,
        provider_model,
        outcome,
        outcome,
        tag,
        tag,
    )
    rows = connection.execute(query, filters + filters).fetchall()
    if len(rows) > 1000:
        raise invalid_request(
            "group_by", "The statistics query returns more than 1000 groups."
        )
    buckets = [
        StatisticsBucket(
            dimensions=row["dimensions"] if dimensions else [],
            calls=row["calls"],
            attempts=row["attempts"],
            units=[UsageItem.model_validate(item) for item in row["units"]],
            cost=_decimal_text(row["cost"]),
            currency=row["currency"].strip(),
        )
        for row in rows
    ]
    return StatisticsResult.model_validate(
        {
            "from": from_time,
            "to": to_time,
            "group_by": list(dimensions),
            "buckets": buckets,
        }
    )


def maintenance_health(
    connection: Connection[Any], *, now: datetime | None = None
) -> tuple[str, str]:
    """Report due price and rollup failures without exposing source details."""
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    configured_price = connection.execute(
        """SELECT 1
           FROM router.provider_models AS mapping
           JOIN router.canonical_models AS model ON model.id = mapping.model_id
           WHERE mapping.price_source IS NOT NULL
              OR (mapping.manual_price IS NULL AND model.price_source IS NOT NULL)
           LIMIT 1"""
    ).fetchone()
    latest_price = connection.execute(
        """SELECT attempted_at, completed FROM router.price_synchronizations
           WHERE run_kind = 'scheduled'
           ORDER BY attempted_at DESC, id DESC LIMIT 1"""
    ).fetchone()
    price_status = "healthy"
    if configured_price is not None and (
        (latest_price is not None and not latest_price["completed"])
        or (
            current.hour >= 3
            and (
                latest_price is None
                or latest_price["attempted_at"].astimezone(UTC).date() < current.date()
            )
        )
    ):
        price_status = "degraded"
    pending_rollup = connection.execute(
        """SELECT 1
           FROM router.raw_accounting_attempts AS attempt
           LEFT JOIN router.accounting_rollups AS rollup
             ON rollup.day = (attempt.started_at AT TIME ZONE 'UTC')::date
           WHERE attempt.started_at <
                 (%s::date::timestamp AT TIME ZONE 'UTC')
           GROUP BY (attempt.started_at AT TIME ZONE 'UTC')::date,
                    rollup.completed_at, rollup.attempt_count
           HAVING rollup.completed_at IS NULL
               OR count(*) <> rollup.attempt_count
               OR max(attempt.recorded_at) > rollup.completed_at
           LIMIT 1""",
        (current.date(),),
    ).fetchone()
    rollup_status = (
        "degraded" if current.hour >= 3 and pending_rollup is not None else "healthy"
    )
    return price_status, rollup_status


def next_daily_run(now: datetime, hour: int) -> datetime:
    """Return the next fixed UTC run time for one daily operation."""
    _require_aware(now)
    if not 0 <= hour <= 23:
        raise ValueError("The daily operation hour is invalid.")
    current = now.astimezone(UTC)
    candidate = datetime.combine(current.date(), time(hour=hour), tzinfo=UTC)
    return candidate if candidate > current else candidate + timedelta(days=1)


def _synchronized_price(value: Price, source: str, now: datetime) -> Price:
    if value.source is not None or value.synchronized_at is not None:
        raise ValueError("A source row cannot supply Router synchronization metadata.")
    units: list[UnitPriceWrite] = []
    for item in value.unit_prices:
        amount = _exact_decimal(item.amount, positive=True)
        units.append(UnitPriceWrite(unit=item.unit, amount=_decimal_text(amount)))
    return Price(
        currency=value.currency,
        unit_prices=units,
        source=source,
        synchronized_at=now,
    )


def _price_snapshot(value: Price) -> AttemptPriceSnapshot:
    return AttemptPriceSnapshot(
        currency=value.currency,
        unit_prices=tuple(
            PriceRate(item.unit, _exact_decimal(item.amount))
            for item in value.unit_prices
        ),
        source=value.source,
        synchronized_at=value.synchronized_at,
    )


def _snapshot_json(value: AttemptPriceSnapshot) -> dict[str, Any]:
    result: dict[str, Any] = {
        "currency": value.currency,
        "unit_prices": [
            {"unit": item.unit, "amount": _decimal_text(item.amount)}
            for item in sorted(value.unit_prices, key=lambda item: item.unit)
        ],
    }
    if value.source is not None:
        result["source"] = value.source
    if value.synchronized_at is not None:
        result["synchronized_at"] = value.synchronized_at.isoformat()
    return result


def _price_values(value: Price) -> tuple[str, tuple[tuple[str, Decimal], ...]]:
    return (
        value.currency,
        tuple(
            sorted(
                (item.unit, _exact_decimal(item.amount)) for item in value.unit_prices
            )
        ),
    )


def _exact_decimal(value: str | Decimal, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool | float):
        raise TypeError("Accounting values cannot use binary floating point.")
    if isinstance(value, str) and len(value) > 64:
        raise ValueError("The accounting decimal value is outside its safe range.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("The accounting decimal value is invalid.") from error
    exponent = result.as_tuple().exponent
    if (
        not result.is_finite()
        or result < 0
        or (positive and result == 0)
        or abs(result) > _MAXIMUM_DECIMAL
        or not isinstance(exponent, int)
        or exponent < -18
    ):
        raise ValueError("The accounting decimal value is outside its safe range.")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("The accounting time must include a time zone.")


def _require_interval(started_at: datetime, completed_at: datetime) -> None:
    _require_aware(started_at)
    _require_aware(completed_at)
    if completed_at < started_at:
        raise ValueError("The accounting interval is invalid.")


def _require_statistics_range(from_time: datetime, to_time: datetime) -> None:
    try:
        _require_aware(from_time)
        _require_aware(to_time)
    except ValueError as error:
        raise invalid_request(
            "from", "Statistics times must include a time zone."
        ) from error
    if to_time <= from_time or to_time - from_time > timedelta(days=366):
        raise invalid_request("to", "Statistics must cover no more than 366 days.")

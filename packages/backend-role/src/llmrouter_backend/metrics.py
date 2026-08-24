"""Bounded Prometheus metrics without request content or control values."""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

import psycopg
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from decimal import Decimal

type CallKind = Literal["model", "embedding", "media"]

_DATABASE_CONNECT_TIMEOUT_SECONDS: Final = 2
_DATABASE_OPTIONS: Final = "-c statement_timeout=2000 -c lock_timeout=500"
_DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    900.0,
    math.inf,
)
_MEDIA_KINDS: Final = ("image", "video", "audio")
_MEDIA_STATES: Final = ("pending", "running", "succeeded", "failed")
_DATABASE_PROBE_LIMIT: Final = 2
_MAXIMUM_PROVIDER_MODEL_SERIES: Final = 1_024
_OTHER_PROVIDER_MODEL: Final = "(other)"
_REQUEST_OUTCOMES: Final = frozenset(
    {
        "succeeded",
        "cancelled",
        "authentication_required",
        "permission_denied",
        "invalid_request",
        "not_found",
        "conflict",
        "assignment_cycle",
        "provider_unavailable",
        "upstream_failed",
        "content_unavailable",
        "rate_limited",
        "internal_error",
    }
)


@dataclass(slots=True)
class _Histogram:
    buckets: list[int] = field(
        default_factory=lambda: [0 for _bucket in _DURATION_BUCKETS]
    )
    count: int = 0
    total: float = 0.0


class MetricsRegistry:
    """Keep one replica's bounded operational counters and saturation state."""

    def __init__(self) -> None:
        """Create one empty replica-local registry."""
        self._lock = threading.RLock()
        self._counters: dict[tuple[str, tuple[str, ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[str, ...]], _Histogram] = {}
        self._database_active = 0
        self._database_limit = 0
        self._database_probe_active = 0
        self._call_active = 0
        self._call_limit = 0
        self._provider_models: set[str] = set()

    def set_database_saturation(self, active: int, limit: int) -> None:
        """Set the current database-request admission state for this replica."""
        with self._lock:
            self._database_active = active
            self._database_limit = limit

    def try_admit_database_request(self, limit: int) -> bool:
        """Admit one database-backed HTTP request without waiting in memory."""
        with self._lock:
            self._database_limit = limit
            if self._database_active >= limit:
                self._increment("database_admission_rejections", ())
                return False
            self._database_active += 1
            return True

    def release_database_request(self) -> None:
        """Release one admitted database-backed HTTP request."""
        with self._lock:
            self._database_active = max(0, self._database_active - 1)

    def set_call_saturation(self, active: int, limit: int) -> None:
        """Set the current call admission state for this replica."""
        with self._lock:
            self._call_active = active
            self._call_limit = limit

    def reject_call(self, kind: CallKind) -> None:
        """Count one safe retryable call-admission rejection."""
        with self._lock:
            self._increment("call_admission_rejections", (kind,))

    def observe_request(self, kind: CallKind, outcome: str, duration: float) -> None:
        """Observe one logical call with only closed operational labels."""
        with self._lock:
            safe_outcome = outcome if outcome in _REQUEST_OUTCOMES else "internal_error"
            labels = (kind, safe_outcome)
            self._increment("requests", labels)
            self._observe("request_duration", labels, duration)

    def observe_attempt(  # noqa: PLR0913 - Closed metric dimensions are explicit.
        self,
        *,
        kind: CallKind,
        provider_model: str,
        outcome: Literal["succeeded", "failed"],
        duration: float,
        usage: Sequence[tuple[str, Decimal]],
        cost: Decimal,
        currency: str,
    ) -> None:
        """Observe one provider attempt after it has produced final facts."""
        with self._lock:
            safe_provider_model = self._provider_model_label(provider_model)
            labels = (kind, safe_provider_model, outcome)
            self._increment("attempts", labels)
            self._observe("attempt_duration", labels, duration)
            for unit, quantity in usage:
                self._increment(
                    "usage_units",
                    (kind, safe_provider_model, unit),
                    _decimal_value(quantity),
                )
            self._increment(
                "cost",
                (kind, safe_provider_model, currency),
                _decimal_value(cost),
            )

    def render(
        self,
        *,
        database_url: str | None,
        cooldowns: Iterable[tuple[str, float, str]],
    ) -> str:
        """Render one Prometheus text snapshot and isolate database failures."""
        with self._lock:
            probe_database = self._database_probe_active < _DATABASE_PROBE_LIMIT
            if probe_database:
                self._database_probe_active += 1
        try:
            media_counts, database_healthy = (
                _database_snapshot(database_url) if probe_database else ({}, False)
            )
        finally:
            if probe_database:
                with self._lock:
                    self._database_probe_active -= 1
        with self._lock:
            counters = dict(self._counters)
            histograms = {
                key: _Histogram(list(value.buckets), value.count, value.total)
                for key, value in self._histograms.items()
            }
            database_active = self._database_active
            database_limit = self._database_limit
            call_active = self._call_active
            call_limit = self._call_limit
            database_probe_active = self._database_probe_active
        return _render_snapshot(
            counters=counters,
            histograms=histograms,
            database_healthy=database_healthy,
            media_counts=media_counts,
            cooldowns=_bounded_cooldowns(cooldowns),
            database_active=database_active,
            database_limit=database_limit,
            call_active=call_active,
            call_limit=call_limit,
            database_probe_active=database_probe_active,
        )

    def _increment(
        self, name: str, labels: tuple[str, ...], amount: float = 1.0
    ) -> None:
        key = (name, labels)
        current = self._counters.get(key, 0.0)
        self._counters[key] = _finite_number(current + _finite_number(amount))

    def _observe(self, name: str, labels: tuple[str, ...], duration: float) -> None:
        value = max(0.0, duration) if math.isfinite(duration) else 0.0
        histogram = self._histograms.setdefault((name, labels), _Histogram())
        histogram.count += 1
        histogram.total += value
        for index, bucket in enumerate(_DURATION_BUCKETS):
            if value <= bucket:
                histogram.buckets[index] += 1

    def _provider_model_label(self, provider_model: str) -> str:
        if provider_model in self._provider_models:
            return provider_model
        if len(self._provider_models) >= _MAXIMUM_PROVIDER_MODEL_SERIES:
            return _OTHER_PROVIDER_MODEL
        self._provider_models.add(provider_model)
        return provider_model


def _database_snapshot(
    database_url: str | None,
) -> tuple[dict[tuple[str, str], int], bool]:
    if database_url is None:
        return {}, False
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=_DATABASE_CONNECT_TIMEOUT_SECONDS,
            options=_DATABASE_OPTIONS,
            row_factory=dict_row,
        ) as connection:
            rows = connection.execute(
                """SELECT kind, state, count(*) AS count
                   FROM router.media_jobs GROUP BY kind, state"""
            ).fetchall()
    except Exception:  # noqa: BLE001 - A scrape reports dependency health as data.
        return {}, False
    return {
        (str(row["kind"]), str(row["state"])): int(row["count"]) for row in rows
    }, True


def _render_snapshot(  # noqa: PLR0913
    *,
    counters: dict[tuple[str, tuple[str, ...]], float],
    histograms: dict[tuple[str, tuple[str, ...]], _Histogram],
    database_healthy: bool,
    media_counts: dict[tuple[str, str], int],
    cooldowns: tuple[tuple[str, float, str], ...],
    database_active: int,
    database_limit: int,
    call_active: int,
    call_limit: int,
    database_probe_active: int,
) -> str:
    attempts_help = (
        "# HELP llmrouter_attempts_total Provider attempts by kind, model, and outcome."
    )
    cooldown_help = (
        "# HELP llmrouter_provider_model_cooldown_seconds Current local cooldown time."
    )
    lines = [
        "# HELP llmrouter_requests_total Logical Router calls by kind and outcome.",
        "# TYPE llmrouter_requests_total counter",
    ]
    _render_counters(
        lines, counters, "requests", "llmrouter_requests_total", ("kind", "outcome")
    )
    lines.extend(
        (
            attempts_help,
            "# TYPE llmrouter_attempts_total counter",
        )
    )
    _render_counters(
        lines,
        counters,
        "attempts",
        "llmrouter_attempts_total",
        ("kind", "provider_model", "outcome"),
    )
    _render_histogram_family(
        lines,
        histograms,
        "request_duration",
        "llmrouter_request_duration_seconds",
        "Logical Router call latency.",
        ("kind", "outcome"),
    )
    _render_histogram_family(
        lines,
        histograms,
        "attempt_duration",
        "llmrouter_attempt_duration_seconds",
        "Provider attempt latency.",
        ("kind", "provider_model", "outcome"),
    )
    lines.extend(
        (
            "# HELP llmrouter_usage_units_total Reported provider usage units.",
            "# TYPE llmrouter_usage_units_total counter",
        )
    )
    _render_counters(
        lines,
        counters,
        "usage_units",
        "llmrouter_usage_units_total",
        ("kind", "provider_model", "unit"),
    )
    lines.extend(
        (
            "# HELP llmrouter_cost_total Snapshotted provider cost by currency.",
            "# TYPE llmrouter_cost_total counter",
        )
    )
    _render_counters(
        lines,
        counters,
        "cost",
        "llmrouter_cost_total",
        ("kind", "provider_model", "currency"),
    )
    lines.extend(
        (
            cooldown_help,
            "# TYPE llmrouter_provider_model_cooldown_seconds gauge",
        )
    )
    for provider_model, remaining, failure_class in sorted(cooldowns):
        labels = _labels(
            (
                ("provider_model", provider_model),
                ("failure_class", failure_class),
            )
        )
        lines.append(
            "llmrouter_provider_model_cooldown_seconds"
            f"{labels} {_number(max(0.0, remaining))}"
        )
    lines.extend(
        (
            "# HELP llmrouter_media_jobs Current media jobs by kind and state.",
            "# TYPE llmrouter_media_jobs gauge",
        )
    )
    lines.extend(
        f"llmrouter_media_jobs{_labels((('kind', kind), ('state', state)))} "
        f"{media_counts.get((kind, state), 0)}"
        for kind in _MEDIA_KINDS
        for state in _MEDIA_STATES
    )
    database_active_line = (
        f'llmrouter_saturation_active{{resource="database_request"}} {database_active}'
    )
    database_limit_line = (
        f'llmrouter_saturation_limit{{resource="database_request"}} {database_limit}'
    )
    database_rejections = _counter_value(counters, "database_admission_rejections", ())
    rejection_help = (
        "# HELP llmrouter_admission_rejections_total Work rejected at a "
        "concurrency limit."
    )
    database_rejection_line = (
        "llmrouter_admission_rejections_total"
        f'{{resource="database_request",kind=""}} {database_rejections}'
    )
    database_probe_active_line = (
        'llmrouter_saturation_active{resource="database_probe"} '
        f"{database_probe_active}"
    )
    database_probe_limit_line = (
        'llmrouter_saturation_limit{resource="database_probe"} '
        f"{_DATABASE_PROBE_LIMIT}"
    )
    lines.extend(
        (
            "# HELP llmrouter_database_healthy PostgreSQL scrape-check health.",
            "# TYPE llmrouter_database_healthy gauge",
            f"llmrouter_database_healthy {int(database_healthy)}",
            "# HELP llmrouter_saturation_active Current admitted work by resource.",
            "# TYPE llmrouter_saturation_active gauge",
            database_active_line,
            f'llmrouter_saturation_active{{resource="call"}} {call_active}',
            database_probe_active_line,
            "# HELP llmrouter_saturation_limit Configured work limit by resource.",
            "# TYPE llmrouter_saturation_limit gauge",
            database_limit_line,
            f'llmrouter_saturation_limit{{resource="call"}} {call_limit}',
            database_probe_limit_line,
            rejection_help,
            "# TYPE llmrouter_admission_rejections_total counter",
            database_rejection_line,
        )
    )
    for (name, label_values), value in sorted(counters.items()):
        if name == "call_admission_rejections":
            labels_text = _labels((("resource", "call"), ("kind", label_values[0])))
            lines.append(
                f"llmrouter_admission_rejections_total{labels_text} {_number(value)}"
            )
    return "\n".join(lines) + "\n"


def _render_counters(
    lines: list[str],
    counters: dict[tuple[str, tuple[str, ...]], float],
    internal_name: str,
    public_name: str,
    label_names: tuple[str, ...],
) -> None:
    for (name, values), value in sorted(counters.items()):
        if name == internal_name:
            lines.append(
                f"{public_name}{_labels(tuple(zip(label_names, values, strict=True)))} "
                f"{_number(value)}"
            )


def _render_histogram_family(  # noqa: PLR0913, PLR0917
    lines: list[str],
    histograms: dict[tuple[str, tuple[str, ...]], _Histogram],
    internal_name: str,
    public_name: str,
    help_text: str,
    label_names: tuple[str, ...],
) -> None:
    lines.extend(
        (f"# HELP {public_name} {help_text}", f"# TYPE {public_name} histogram")
    )
    for (name, values), histogram in sorted(histograms.items()):
        if name != internal_name:
            continue
        base_labels = tuple(zip(label_names, values, strict=True))
        for bucket, count in zip(_DURATION_BUCKETS, histogram.buckets, strict=True):
            boundary = "+Inf" if math.isinf(bucket) else _number(bucket)
            bucket_labels = _labels((*base_labels, ("le", boundary)))
            lines.append(f"{public_name}_bucket{bucket_labels} {count}")
        lines.append(f"{public_name}_count{_labels(base_labels)} {histogram.count}")
        lines.append(
            f"{public_name}_sum{_labels(base_labels)} {_number(histogram.total)}"
        )


def _counter_value(
    counters: dict[tuple[str, tuple[str, ...]], float],
    name: str,
    labels: tuple[str, ...],
) -> str:
    return _number(counters.get((name, labels), 0.0))


def _labels(values: tuple[tuple[str, str], ...]) -> str:
    if not values:
        return ""
    rendered = ",".join(f'{name}="{_escape_label(value)}"' for name, value in values)
    return "{" + rendered + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    safe_value = _finite_number(value)
    if safe_value == 0:
        return "0"
    return format(safe_value, ".17g")


def _decimal_value(value: Decimal) -> float:
    return _finite_number(float(value))


def _finite_number(value: float) -> float:
    if math.isnan(value) or value < 0:
        return 0.0
    if math.isinf(value) or value > sys.float_info.max:
        return sys.float_info.max
    return value


def _bounded_cooldowns(
    values: Iterable[tuple[str, float, str]],
) -> tuple[tuple[str, float, str], ...]:
    selected = sorted(values)
    if len(selected) <= _MAXIMUM_PROVIDER_MODEL_SERIES:
        return tuple(selected)
    overflow = selected[_MAXIMUM_PROVIDER_MODEL_SERIES:]
    return (
        *selected[:_MAXIMUM_PROVIDER_MODEL_SERIES],
        (
            _OTHER_PROVIDER_MODEL,
            max(_finite_number(value[1]) for value in overflow),
            "upstream_failed",
        ),
    )

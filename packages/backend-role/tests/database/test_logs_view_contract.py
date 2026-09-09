"""Prove the accepted Logs API page and retention boundaries."""
# ruff: noqa: PLR2004

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

from .test_detailed_logs import LogContext

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import tzinfo
    from pathlib import Path

    import httpx

NOW = datetime(2026, 8, 29, 12, 34, 56, tzinfo=UTC)
LOWER = NOW - timedelta(days=7)
BOUNDS = {"from": "2026-08-22T12:34:56Z", "to": "2026-08-29T12:34:56Z"}
LOGS = "/v1/admin/request-logs"
REQUEST = '{"messages":[{"role":"user","content":"Synthetic boundary input."}]}'
RESPONSE = '{"content":"Synthetic boundary output."}'


@pytest.fixture
def logs_context(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LogContext]:
    """Fix both retention clocks only inside this test and its temporary database."""

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr("llmrouter_backend.diagnostics.datetime", FixedDatetime)
    # Resolve only this database's unqualified clock calls to a fixed instant.
    # Keep the real retention predicates and cleanup active. Do not change the
    # PostgreSQL server clock, pg_catalog, or the configured retention duration.
    with psycopg.connect(database_url) as connection:
        assert connection.info.dbname.startswith("llmrouter_test_")
        connection.execute("CREATE SCHEMA logs_test_clock")
        connection.execute(
            sql.SQL(
                """CREATE FUNCTION logs_test_clock.statement_timestamp()
                   RETURNS timestamptz LANGUAGE sql STABLE
                   AS {}"""
            ).format(sql.Literal(f"SELECT TIMESTAMPTZ '{NOW.isoformat()}'"))
        )
        connection.execute(
            sql.SQL(
                "ALTER DATABASE {} SET search_path = logs_test_clock, pg_catalog"
            ).format(sql.Identifier(connection.info.dbname))
        )
    real_before = datetime.now(tz=UTC)
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT statement_timestamp()").fetchone() == (NOW,)
        real_clock = connection.execute(
            "SELECT pg_catalog.statement_timestamp()"
        ).fetchone()
        assert real_clock is not None
        assert real_before <= real_clock[0] <= datetime.now(tz=UTC)
    # The administrator database on the same server keeps normal resolution.
    with psycopg.connect(os.environ["LLMROUTER_TEST_DATABASE_URL"]) as connection:
        assert (
            "logs_test_clock"
            not in connection.execute("SHOW search_path").fetchone()[0]
        )
        other_clock = connection.execute("SELECT statement_timestamp()").fetchone()
        assert other_clock is not None
        assert real_before <= other_clock[0] <= datetime.now(tz=UTC)

    context = LogContext(database_url, tmp_path)
    try:
        retention = context.client.get(
            "/v1/admin/settings/log-retention", headers=context.read_headers
        )
        assert retention.status_code == HTTPStatus.OK
        assert retention.json() == {"duration_days": 7}
        yield context
    finally:
        context.client.close()


def _seed(
    context: LogContext,
    records: list[tuple[uuid.UUID, datetime]],
    *,
    administrator: str | None = None,
    configuration_service: str | None = None,
) -> None:
    """Store synthetic calls and logs without a provider or a log response fixture."""
    actor = "service" if administrator is None else "administrator"
    service_id = context.service_id if administrator is None else None
    workspace_id = context.workspace_id if administrator is None else None
    assignment = "workflow" if configuration_service is not None else None
    with psycopg.connect(context.database_url) as connection:
        for log_id, started_at in records:
            call_id = uuid.uuid4()
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                       (id, call_actor, service_id, workspace_id,
                        administrator_subject, configuration_service_api_name,
                        assignment_api_name, exact_provider_model_api_name,
                        outcome, started_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           'succeeded', %s, %s)""",
                (
                    call_id,
                    actor,
                    service_id,
                    workspace_id,
                    administrator,
                    configuration_service,
                    assignment,
                    "fake-model" if assignment is None else None,
                    started_at,
                    started_at,
                ),
            )
            connection.execute(
                """INSERT INTO router.request_logs
                       (id, logical_call_id, call_actor, service_id, workspace_id,
                        administrator_subject, configuration_service_api_name,
                        assignment_api_name, provider_model_api_name, kind,
                        outcome, request_json, response_json, started_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'fake-model',
                           'model', 'succeeded', %s, %s, %s)""",
                (
                    log_id,
                    call_id,
                    actor,
                    service_id,
                    workspace_id,
                    administrator,
                    configuration_service,
                    assignment,
                    REQUEST,
                    RESPONSE,
                    started_at,
                ),
            )


def _ordered_records(count: int) -> list[tuple[uuid.UUID, datetime]]:
    # Construct the expected order directly: three records at each instant.
    # Older groups have larger IDs, so ID order alone must fail. Within a group,
    # IDs descend. The 100-record page ends inside a group, not between groups.
    return [
        (
            uuid.UUID(int=(index // 3 + 1) * 10 + 3 - index % 3),
            LOWER if index == count - 1 else NOW - timedelta(seconds=1 + index // 3),
        )
        for index in range(count)
    ]


def _page(
    context: LogContext, parameters: dict[str, str]
) -> tuple[httpx.Response, dict[str, Any]]:
    response = context.client.get(LOGS, params=parameters, headers=context.read_headers)
    assert response.status_code == HTTPStatus.OK
    assert response.headers["cache-control"] == "no-store"
    assert dict(response.request.url.params) == parameters
    return response, response.json()


def _walk(
    context: LogContext,
    expected: list[tuple[uuid.UUID, datetime]],
    *,
    limit: int = 100,
    filters: dict[str, str] | None = None,
) -> None:
    parameters = {**BOUNDS, "limit": str(limit), **(filters or {})}
    seen: list[str] = []
    cursors: set[str] = set()
    for start in range(0, max(1, len(expected)), limit):
        _, page = _page(context, parameters)
        selected = expected[start : start + limit]
        assert [item["id"] for item in page["items"]] == [
            str(log_id) for log_id, _ in selected
        ]
        assert [
            datetime.fromisoformat(item["started_at"]) for item in page["items"]
        ] == [started_at for _, started_at in selected]
        seen.extend(item["id"] for item in page["items"])
        more = start + limit < len(expected)
        assert page["page"]["has_more"] is more
        if more:
            cursor = page["page"]["next_cursor"]
            assert isinstance(cursor, str)
            assert 1 <= len(cursor) <= 500
            assert cursor not in cursors
            cursors.add(cursor)
            # Use the opaque server value unchanged. Do not derive it from IDs.
            parameters = {**parameters, "cursor": cursor}
        else:
            assert "next_cursor" not in page["page"]
    assert len(seen) == len(set(seen)) == len(expected)


@pytest.mark.parametrize("count", [0, 37, 100, 203])
def test_logs_first_page_and_cursor_boundaries(
    logs_context: LogContext, count: int
) -> None:
    """Read zero, fewer than 100, exactly 100, and more than 100 retained logs."""
    context = logs_context
    expected = _ordered_records(count)
    # Insert in a different order from both the expected timestamps and IDs.
    _seed(context, expected[::2] + expected[1::2])
    excluded = [
        (uuid.UUID(int=9001), LOWER - timedelta(microseconds=1)),
        (uuid.UUID(int=9002), NOW),
        (uuid.UUID(int=9003), NOW + timedelta(microseconds=1)),
    ]
    _seed(context, excluded)
    _walk(context, expected)

    # The expired control is removed by real cleanup. T and T+1us still exist,
    # so their absence above proves the exclusive query bound.
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT id FROM router.request_logs WHERE id = ANY(%s) ORDER BY id",
            ([log_id for log_id, _ in excluded],),
        ).fetchall() == [(excluded[1][0],), (excluded[2][0],)]
    if expected:
        assert expected[-1][1] == LOWER


@pytest.mark.parametrize("limit", [1, 200])
def test_logs_keep_public_limit_compatibility(
    logs_context: LogContext, limit: int
) -> None:
    """Walk valid smallest and largest API pages without loss at tied times."""
    expected = _ordered_records(203)
    _seed(logs_context, list(reversed(expected)))
    _walk(logs_context, expected, limit=limit)


@pytest.mark.parametrize(
    ("filters", "administrator", "configuration_service"),
    [
        ({"call_actor": "service"}, None, None),
        ({"call_actor": "administrator"}, "selected-administrator", "alpha"),
        (
            {"administrator": "selected-administrator"},
            "selected-administrator",
            "alpha",
        ),
        ({"configuration_service": "alpha"}, "selected-administrator", "alpha"),
        (
            {
                "call_actor": "administrator",
                "administrator": "selected-administrator",
                "configuration_service": "alpha",
            },
            "selected-administrator",
            "alpha",
        ),
    ],
)
def test_logs_filters_keep_the_same_cursor_walk(
    logs_context: LogContext,
    filters: dict[str, str],
    administrator: str | None,
    configuration_service: str | None,
) -> None:
    """Keep each active filter on later pages, with nonmatching tied controls."""
    context = logs_context
    expected = _ordered_records(103)
    _seed(
        context,
        list(reversed(expected)),
        administrator=administrator,
        configuration_service=configuration_service,
    )
    # Put nonmatching records on both sides of the page boundary. Their larger
    # tied IDs expose a dropped filter on either the first or the later page.
    controls = [(NOW - timedelta(seconds=1)), (NOW - timedelta(seconds=35))]
    if filters == {"call_actor": "administrator"}:
        excluded_actors = [(None, None)]
    elif filters == {"call_actor": "service"}:
        excluded_actors = [("other-administrator", "beta")]
    elif filters == {"administrator": "selected-administrator"}:
        excluded_actors = [(None, None), ("other-administrator", "alpha")]
    elif filters == {"configuration_service": "alpha"}:
        excluded_actors = [
            (None, None),
            ("selected-administrator", "beta"),
            ("selected-administrator", None),
        ]
    else:
        excluded_actors = [
            (None, None),
            ("other-administrator", "alpha"),
            ("selected-administrator", "beta"),
        ]
    for index, (subject, service) in enumerate(excluded_actors):
        _seed(
            context,
            [
                (uuid.UUID(int=9000 + index * 10 + position), instant)
                for position, instant in enumerate(controls)
            ],
            administrator=subject,
            configuration_service=service,
        )
    _walk(context, expected, filters=filters)


def test_logs_detail_and_authentication_boundaries(logs_context: LogContext) -> None:
    """Read retained details; deny anonymous and service reads and missing data."""
    context = logs_context
    retained = uuid.UUID(int=1)
    expired = uuid.UUID(int=2)
    missing = uuid.UUID(int=3)
    _seed(context, [(retained, LOWER), (expired, LOWER - timedelta(microseconds=1))])
    for path in (LOGS, f"{LOGS}/{retained}"):
        for headers in ({}, {"Authorization": f"Bearer {context.service_key}"}):
            denied = context.client.get(
                path, params={**BOUNDS, "limit": "100"}, headers=headers
            )
            assert denied.status_code == HTTPStatus.UNAUTHORIZED
            assert denied.json()["error"]["code"] == "authentication_required"

    _, page = _page(context, {**BOUNDS, "limit": "100"})
    selected = page["items"][0]
    assert selected["id"] == str(retained)
    detail = context.client.get(f"{LOGS}/{retained}", headers=context.read_headers)
    assert detail.status_code == HTTPStatus.OK
    assert detail.headers["cache-control"] == "no-store"
    assert detail.json()["summary"] == selected
    assert detail.json()["request_json"] == REQUEST
    assert detail.json()["response_json"] == RESPONSE
    assert detail.json()["attempts"] == []

    with psycopg.connect(context.database_url) as connection:
        connection.execute("DELETE FROM router.request_logs WHERE id = %s", (retained,))
    for log_id in (retained, expired, missing):
        unavailable = context.client.get(
            f"{LOGS}/{log_id}", headers=context.read_headers
        )
        assert unavailable.status_code == HTTPStatus.NOT_FOUND
        assert unavailable.json()["error"]["code"] == "not_found"


def test_logs_custom_time_bounds_keep_retained_controls(
    logs_context: LogContext,
) -> None:
    """Exclude retained rows outside a narrower inclusive/exclusive query range."""
    context = logs_context
    start = NOW - timedelta(hours=2)
    stop = NOW - timedelta(hours=1)
    records = [
        (uuid.UUID(int=1), start - timedelta(microseconds=1)),
        (uuid.UUID(int=2), start),
        (uuid.UUID(int=3), stop - timedelta(microseconds=1)),
        (uuid.UUID(int=4), stop),
    ]
    _seed(context, records)
    parameters = {
        "from": "2026-08-29T10:34:56Z",
        "to": "2026-08-29T11:34:56Z",
        "limit": "100",
    }
    _, page = _page(context, parameters)
    assert [item["id"] for item in page["items"]] == [
        str(records[2][0]),
        str(records[1][0]),
    ]
    assert page["page"] == {"has_more": False}
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.request_logs"
        ).fetchone() == (4,)

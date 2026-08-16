"""PostgreSQL model-request view tests."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
    ServicePrincipal,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import ExecutionKind, ExecutionTarget
from llmrouter_backend.model_requests.repository import PostgresModelRequestViews

from .helpers import (
    OTHER_SERVICE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def _principal(
    service_id: str = SERVICE_ID,
    allowed_workspaces: frozenset[str] | None = frozenset({WORKSPACE_ID}),
) -> ServicePrincipal:
    return ServicePrincipal(
        "test",
        "token-id",
        Audience.DATA_PLANE,
        service_id,
        frozenset({"model.read", "model.cancel"}),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=4),
        1,
        allowed_workspaces,
    )


def _context(operation: str = "model.read") -> RequestContext:
    return RequestContext(
        "transport-request",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.DATA_PLANE,
        operation,
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        operation in {"model.create", "model.cancel"},
    )


def test_views_hide_other_scopes_and_return_bounded_zero_accounting(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)

    views = PostgresModelRequestViews(database_url)
    assert views.resolve_scope(_principal(), REQUEST_ID) == Scope(
        SERVICE_ID, WORKSPACE_ID
    )
    assert views.resolve_scope(_principal(OTHER_SERVICE_ID, None), REQUEST_ID) is None
    assert views.resolve_scope(_principal(SERVICE_ID, frozenset()), REQUEST_ID) is None

    status = views.status(_context(), ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID))
    assert status["request_id"] == REQUEST_ID
    assert status["state"] == "admitted"
    assert status["attempts"] == []
    assert status["accounting"] == {
        "estimated": "0",
        "reserved": "0",
        "used": "0",
        "corrected": "0",
        "currency": "USD",
    }
    assert "fingerprint" not in status
    assert "credential" not in status
    point = views.resume_point(
        _context("model.create"), ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    )
    assert point.state.value == "admitted"
    assert point.state_revision == 1

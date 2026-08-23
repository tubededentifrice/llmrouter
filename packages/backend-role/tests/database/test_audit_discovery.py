"""PostgreSQL checks for stable and redacted global audit discovery."""
# ruff: noqa: D103

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from llmrouter_backend.admin_auth import AdministratorAuthError
from llmrouter_backend.administration import (
    AuditDiscoveryError,
    PostgresAuditRepository,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.database import migrate

from .helpers import SERVICE_ID, WORKSPACE_ID, seed_scope

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
PAGE_SIZE = 100
CURSOR_MAX_LENGTH = 1_000
ACTIVE_TRANSACTION_COUNT = 96
CURSOR_KEY = b"audit-cursor-test-key-material-32"


def _context(
    *,
    authority_class: AuthorityClass = AuthorityClass.GLOBAL_ADMINISTRATOR,
    scope: Scope | None = None,
    operation: str = "audit.read",
) -> RequestContext:
    return RequestContext(
        request_id="audit-discovery",
        actor_kind=(
            PrincipalKind.ADMINISTRATOR
            if authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            else PrincipalKind.SERVICE
        ),
        actor_id="administrator-1",
        authority_class=authority_class,
        authority_path=(
            AuthorityPath.GLOBAL_ADMINISTRATION
            if authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            else AuthorityPath.MACHINE
        ),
        machine_audience=(
            None
            if authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR
            else Audience.CONFIGURATION
        ),
        operation=operation,
        scope=Scope() if scope is None else scope,
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=False,
    )


def _insert_event(  # noqa: PLR0913
    connection: psycopg.Connection[tuple[object, ...]],
    identity: uuid.UUID,
    occurred_at: datetime,
    *,
    actor_id: str = "administrator-1",
    action: str = "service.manage",
    safe_details: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO router.audit_events (
            event_id, audit_class, actor_kind, actor_id, authority_class,
            service_id, workspace_id, action, permission_result,
            safe_details, occurred_at
        ) VALUES (
            %s, 'global_administration', 'administrator', %s,
            'global_administrator', %s, %s, %s, 'permitted',
            %s::jsonb, %s
        )
        """,
        (
            identity,
            actor_id,
            SERVICE_ID,
            WORKSPACE_ID,
            action,
            json.dumps(safe_details or {}),
            occurred_at,
        ),
    )


def test_audit_pages_are_stable_bounded_and_exclude_late_commits(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        for index in range(101):
            _insert_event(
                connection,
                uuid.UUID(int=1_000 + index),
                NOW - timedelta(seconds=index),
            )
    repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)

    with psycopg.connect(database_url) as pending:
        _insert_event(
            pending,
            uuid.UUID(int=2_000),
            NOW - timedelta(seconds=100),
        )
        first, cursor = repository.list_events(
            _context(), start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
        )
        pending.commit()
    assert len(first) == PAGE_SIZE
    assert cursor is not None
    assert first[0]["event_id"] == str(uuid.UUID(int=1_000))

    with psycopg.connect(database_url, autocommit=True) as connection:
        _insert_event(connection, uuid.UUID(int=2_001), NOW - timedelta(seconds=100))
    restarted_repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)
    second, next_cursor = restarted_repository.list_events(
        _context(),
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=1),
        cursor=cursor,
    )

    assert [item["event_id"] for item in second] == [str(uuid.UUID(int=1_100))]
    assert next_cursor is None


def test_audit_cursor_stays_within_contract_with_many_active_transactions(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        for index in range(101):
            _insert_event(
                connection,
                uuid.UUID(int=6_000 + index),
                NOW - timedelta(seconds=index),
            )
    active = [
        psycopg.connect(database_url) for _index in range(ACTIVE_TRANSACTION_COUNT)
    ]
    try:
        for connection in active:
            connection.execute("SELECT pg_current_xact_id()")
        repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)

        _items, cursor = repository.list_events(
            _context(), start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
        )

        assert cursor is not None
        assert len(cursor) <= CURSOR_MAX_LENGTH
        second, next_cursor = repository.list_events(
            _context(),
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=1),
            cursor=cursor,
        )
        assert [item["event_id"] for item in second] == [str(uuid.UUID(int=6_100))]
        assert next_cursor is None
    finally:
        for connection in active:
            connection.rollback()
            connection.close()


def test_audit_discovery_has_exact_global_order_index(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        definition = connection.execute(
            """
            SELECT index.indisvalid,
                   index.indisready,
                   index.indnkeyatts,
                   pg_get_indexdef(index.indexrelid)
            FROM pg_index AS index
            WHERE index.indexrelid =
                  'router.audit_events_global_discovery_idx'::regclass
            """
        ).fetchone()

    assert definition == (
        True,
        True,
        2,
        (
            "CREATE INDEX audit_events_global_discovery_idx ON "
            "router.audit_events USING btree (occurred_at DESC, event_id DESC)"
        ),
    )


def test_audit_discovery_rejects_changed_cursor_range_and_non_global_context(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        for index in range(101):
            _insert_event(
                connection,
                uuid.UUID(int=3_000 + index),
                NOW - timedelta(seconds=index),
            )
    repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)
    start = NOW - timedelta(days=1)
    end = NOW + timedelta(days=1)
    _items, cursor = repository.list_events(_context(), start=start, end=end)
    assert cursor is not None

    with pytest.raises(AuditDiscoveryError, match="cursor"):
        repository.list_events(
            _context(), start=start - timedelta(seconds=1), end=end, cursor=cursor
        )
    with pytest.raises(AuditDiscoveryError, match="cursor"):
        repository.list_events(
            _context(),
            start=start,
            end=end,
            cursor=f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}",
        )
    with pytest.raises(AuditDiscoveryError, match="range"):
        repository.list_events(_context(), start=end, end=start)
    with pytest.raises(AdministratorAuthError) as denied:
        repository.list_events(
            _context(
                authority_class=AuthorityClass.SERVICE,
                scope=Scope(SERVICE_ID, WORKSPACE_ID),
            ),
            start=start,
            end=end,
        )
    assert denied.value.code == "insufficient_scope"


def test_audit_discovery_returns_only_closed_redacted_detail(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        _insert_event(
            connection,
            uuid.UUID(int=4_000),
            NOW,
            actor_id="Bearer private-actor-token",
            action="private.prompt",
            safe_details={
                "resource_type": "service",
                "resource_id": SERVICE_ID,
                "reason": "Q7w9K2m4Z8x1V6b3N5c0R2t8Y4p7L9d1",
                "safe_error_code": "internal_error",
                "prompt": "private prompt",
                "output": "private output",
                "credential": "private credential",
                "token": "private token",
                "provider_error": {"body": "private body"},
            },
        )
    repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)

    items, cursor = repository.list_events(
        _context(), start=NOW - timedelta(seconds=1), end=NOW + timedelta(seconds=1)
    )

    assert cursor is None
    assert items == (
        {
            "event_id": str(uuid.UUID(int=4_000)),
            "occurred_at": NOW.isoformat(),
            "actor": (
                "administrator:"
                + hmac.digest(
                    CURSOR_KEY,
                    b"llmrouter-audit-actor-v1\0"
                    b"administrator\0Bearer private-actor-token",
                    hashlib.sha256,
                ).hex()[:16]
            ),
            "action": "unknown",
            "outcome": "permitted",
            "scope": {
                "authority_class": "global_administrator",
                "service_id": SERVICE_ID,
                "workspace_id": WORKSPACE_ID,
            },
            "safe_detail": {
                "resource_type": "service",
                "resource_id": SERVICE_ID,
                "safe_error_code": "internal_error",
            },
        },
    )
    serialized = json.dumps(items)
    assert "private" not in serialized
    assert "prompt" not in serialized
    assert "provider_error" not in serialized


def test_audit_discovery_records_each_permitted_read(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)

    repository = PostgresAuditRepository(database_url, cursor_key=CURSOR_KEY)
    items, cursor = repository.list_events(
        _context(), start=NOW - timedelta(seconds=1), end=NOW + timedelta(seconds=1)
    )

    assert items == ()
    assert cursor is None
    with psycopg.connect(database_url) as connection:
        audit = connection.execute(
            """
            SELECT actor_kind, actor_id, authority_class, action,
                   permission_result, safe_details, occurred_at
            FROM router.audit_events
            WHERE action = 'audit.read'
            """
        ).fetchone()
    assert audit == (
        "administrator",
        "administrator-1",
        "global_administrator",
        "audit.read",
        "permitted",
        {},
        NOW,
    )

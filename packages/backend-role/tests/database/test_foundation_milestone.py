"""End-to-end foundation checks against one real PostgreSQL service."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    SafeAuthorityError,
    Scope,
    ScopeKind,
    authorize,
    resolve_and_lookup,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.testing import ScopeTestBuilder

from .helpers import OTHER_SERVICE_ID, SERVICE_ID, WORKSPACE_ID, seed_scope

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
AUDIT_EVENT_ID = "0198a080-0000-7000-8000-000000000090"


def configuration_policy(operation: str) -> OperationPolicy:
    """Create one workspace configuration policy."""
    return OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.CONFIGURATION,
        principal_kinds=frozenset((PrincipalKind.SERVICE,)),
        scope_kind=ScopeKind.WORKSPACE,
        mutation=operation == "configuration.write",
    )


def test_scope_denial_happens_before_database_lookup(database_url: str) -> None:
    """Do not query a record when the service scope is not authorized."""
    authorized_scope = Scope(SERVICE_ID, WORKSPACE_ID)
    principal = ScopeTestBuilder(authorized_scope, now=NOW).service(
        "configuration.read",
        audience=Audience.CONFIGURATION,
    )
    lookup_count = 0

    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)

        def lookup() -> tuple[str] | None:
            nonlocal lookup_count
            lookup_count += 1
            return connection.execute(
                "SELECT stable_name FROM router.services WHERE id = %s",
                (OTHER_SERVICE_ID,),
            ).fetchone()

        with pytest.raises(SafeAuthorityError):
            resolve_and_lookup(
                principal,
                configuration_policy("configuration.read"),
                Scope(OTHER_SERVICE_ID, WORKSPACE_ID),
                lookup,
                request_id="foundation-denied-lookup",
                now=NOW,
            )

    assert lookup_count == 0


def test_state_and_linked_audit_event_commit_or_roll_back_together(
    database_url: str,
) -> None:
    """Keep one authorized state change and its audit event in one transaction."""
    scope = Scope(SERVICE_ID, WORKSPACE_ID)
    principal = ScopeTestBuilder(scope, now=NOW).service(
        "configuration.write",
        audience=Audience.CONFIGURATION,
    )
    context = authorize(
        principal,
        configuration_policy("configuration.write"),
        scope,
        request_id="foundation-audit-link",
        now=NOW,
    )

    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.commit()

        def attempt_invalid_transaction() -> None:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE router.workspaces
                    SET state = 'disabled', state_revision = 2
                    WHERE id = %s AND service_id = %s
                    """,
                    (WORKSPACE_ID, SERVICE_ID),
                )
                connection.execute(
                    """
                    INSERT INTO router.audit_events (
                        event_id, audit_class, actor_kind, actor_id, authority_class,
                        service_id, workspace_id, action, permission_result, occurred_at
                    ) VALUES (
                        %s, 'security', %s, %s, %s, %s, %s, %s, 'invalid', %s
                    )
                    """,
                    (
                        AUDIT_EVENT_ID,
                        context.actor_kind.value,
                        context.actor_id,
                        context.authority_class.value,
                        context.scope.service_id,
                        context.scope.workspace_id,
                        context.operation,
                        context.authorized_at,
                    ),
                )

        with pytest.raises(psycopg.errors.CheckViolation):
            attempt_invalid_transaction()

        assert connection.execute(
            "SELECT state, state_revision FROM router.workspaces WHERE id = %s",
            (WORKSPACE_ID,),
        ).fetchone() == ("active", 1)
        assert connection.execute(
            "SELECT count(*) FROM router.audit_events WHERE event_id = %s",
            (AUDIT_EVENT_ID,),
        ).fetchone() == (0,)
        connection.commit()

        with connection.transaction():
            connection.execute(
                """
                UPDATE router.workspaces
                SET state = 'disabled', state_revision = 2
                WHERE id = %s AND service_id = %s
                """,
                (WORKSPACE_ID, SERVICE_ID),
            )
            connection.execute(
                """
                INSERT INTO router.audit_events (
                    event_id, audit_class, actor_kind, actor_id, authority_class,
                    service_id, workspace_id, action, permission_result, occurred_at
                ) VALUES (
                    %s, 'security', %s, %s, %s, %s, %s, %s, 'permitted', %s
                )
                """,
                (
                    AUDIT_EVENT_ID,
                    context.actor_kind.value,
                    context.actor_id,
                    context.authority_class.value,
                    context.scope.service_id,
                    context.scope.workspace_id,
                    context.operation,
                    context.authorized_at,
                ),
            )

    with psycopg.connect(database_url, autocommit=True) as connection:
        assert connection.execute(
            """
            SELECT workspace.state, workspace.state_revision, audit.actor_kind,
                   audit.actor_id, audit.authority_class, audit.action,
                   audit.permission_result
            FROM router.workspaces AS workspace
            JOIN router.audit_events AS audit
              ON audit.service_id = workspace.service_id
             AND audit.workspace_id = workspace.id
            WHERE workspace.id = %s AND audit.event_id = %s
            """,
            (WORKSPACE_ID, AUDIT_EVENT_ID),
        ).fetchone() == (
            "disabled",
            2,
            context.actor_kind.value,
            context.actor_id,
            context.authority_class.value,
            context.operation,
            "permitted",
        )


@pytest.mark.parametrize("authority_class", [None, "unknown"])
def test_audit_event_requires_known_authority_class(
    database_url: str, authority_class: str | None
) -> None:
    """Require one authority class from the formal audit scope."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        with pytest.raises(psycopg.errors.IntegrityError):
            connection.execute(
                """
                INSERT INTO router.audit_events (
                    event_id, audit_class, actor_kind, actor_id, authority_class,
                    service_id, action, permission_result, occurred_at
                ) VALUES (
                    %s, 'security', 'service', 'test-service', %s,
                    %s, 'configuration.write', 'denied', %s
                )
                """,
                (AUDIT_EVENT_ID, authority_class, SERVICE_ID, NOW),
            )

"""Isolated PostgreSQL tests for basic administration repositories."""
# ruff: noqa: D103, FBT003

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from Crypto.PublicKey import RSA
from llmrouter_backend.accounting import AccountingError, PostgresAccountingRepository
from llmrouter_backend.admin_auth import (
    AdministratorAuthError,
    AdministratorAuthRepository,
    OIDCConfiguration,
    OIDCTokenVerifier,
)
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    Scope,
    ScopeKind,
)
from llmrouter_backend.configuration import (
    ConfigurationError,
    ConfigurationErrorCode,
    ConfigurationScope,
    PostgresConfigurationRepository,
    ScopeConfiguration,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.execution import (
    ExecutionError,
    ExecutionKind,
    ExecutionState,
    ExecutionTarget,
    PostgresExecutionRepository,
)
from llmrouter_backend.lifecycle import (
    LifecycleError,
    PostgresLifecycleRepository,
    ServiceRecord,
)
from llmrouter_backend.model_requests import PostgresModelRequestViews

from .helpers import (
    OTHER_SERVICE_ID,
    REQUEST_ID,
    REQUEST_ROW_ID,
    SERVICE_ID,
    WORKSPACE_ID,
    insert_request,
    seed_scope,
)
from .test_accounting_repository import _record_budget_limit
from .test_administrator_auth import (
    CLIENT_ID,
    DIGEST_KEY,
    ENCRYPTION_KEY,
    ISSUER,
    ORIGIN,
    REDIRECT,
    FakeIdentityService,
    _bootstrap,
)
from .test_administrator_auth import (
    NOW as AUTH_NOW,
)
from .test_configuration_repository import _registry

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


@pytest.fixture
def administration_auth(
    database_url: str,
) -> tuple[AdministratorAuthRepository, FakeIdentityService, str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    private_key = RSA.generate(2048)
    identity = FakeIdentityService(private_key)
    configuration = OIDCConfiguration(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        authorization_endpoint=f"{ISSUER}/authorize?fixed=1",
        redirect_uri=REDIRECT,
        account_url=f"{ISSUER}/account",
        signing_algorithm="RS256",
    )
    repository = AdministratorAuthRepository(
        database_url,
        configuration=configuration,
        identity_service=identity,
        token_verifier=OIDCTokenVerifier(
            configuration, {"identity-key": private_key.public_key().export_key()}
        ),
        digest_key=DIGEST_KEY,
        encryption_key=ENCRYPTION_KEY,
        exact_origin=ORIGIN,
        trusted_grant_base_url=f"{ORIGIN}/trusted-grant",
    )
    return repository, identity, database_url


def _administrator_context(
    operation: str,
    *,
    service_id: str = SERVICE_ID,
    workspace_id: str | None = None,
    mutation: bool = False,
) -> RequestContext:
    return RequestContext(
        request_id=f"administration-{operation}",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="issuer:administrator",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation=operation,
        scope=Scope(service_id, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=mutation,
    )


def _machine_context(operation: str) -> RequestContext:
    return RequestContext(
        "machine-request",
        PrincipalKind.SERVICE,
        SERVICE_ID,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.DATA_PLANE,
        operation,
        Scope(SERVICE_ID, WORKSPACE_ID),
        NOW,
        None,
        True,
    )


def _embed_context(
    operation: str, *, workspace_id: str | None = None
) -> RequestContext:
    return RequestContext(
        f"embed-{operation}",
        PrincipalKind.EMBED,
        "embed-session",
        AuthorityClass.SERVICE,
        AuthorityPath.EMBED,
        None,
        operation,
        Scope(SERVICE_ID, workspace_id),
        NOW,
        None,
        False,
    )


def test_configuration_write_idempotency_is_durable_and_conflict_safe(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
    repository = PostgresConfigurationRepository(
        database_url, schema_registry=_registry()
    )
    context = _administrator_context("assignment.manage", mutation=True)
    first = repository.publish(
        context,
        ConfigurationScope(SERVICE_ID),
        ScopeConfiguration(),
        expected_active_revision=None,
        reason="Create the service configuration",
        now=NOW,
        resource_id="configuration",
        idempotency_key="configuration-key-0001",
    )
    replay = repository.publish(
        context,
        ConfigurationScope(SERVICE_ID),
        ScopeConfiguration(),
        expected_active_revision=None,
        reason="Create the service configuration",
        now=NOW,
        resource_id="configuration",
        idempotency_key="configuration-key-0001",
    )
    assert replay == first

    with pytest.raises(ConfigurationError) as conflict:
        repository.publish(
            context,
            ConfigurationScope(SERVICE_ID),
            ScopeConfiguration(),
            expected_active_revision=None,
            reason="Use different content with the same key",
            now=NOW,
            resource_id="configuration",
            idempotency_key="configuration-key-0001",
        )
    assert conflict.value.code is ConfigurationErrorCode.IDEMPOTENCY_CONFLICT
    with psycopg.connect(database_url) as connection:
        counts = connection.execute(
            """SELECT
                   (SELECT count(*)
                    FROM router.configuration_write_idempotency_bindings),
                   (SELECT count(*) FROM router.configuration_revisions
                    WHERE scope_kind = 'service' AND service_id = %s),
                   (SELECT count(*) FROM router.audit_events
                    WHERE action = 'configuration.publish')""",
            (SERVICE_ID,),
        ).fetchone()
    assert counts == (1, 1, 1)


def test_human_repository_reads_are_exact_scope_and_content_free(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        insert_request(connection, REQUEST_ROW_ID, REQUEST_ID)

    target = ExecutionTarget(ExecutionKind.MODEL, REQUEST_ID)
    execution = PostgresExecutionRepository(database_url)
    machine = _machine_context("model.create")
    execution.transition(
        machine, target, expected_revision=1, new_state=ExecutionState.RUNNING
    )
    execution.append_event(
        machine,
        target,
        event_name="output.delta",
        payload={
            "output_index": 0,
            "content_type": "text/plain",
            "delta": "private retained output",
        },
    )
    execution.append_event(
        machine,
        target,
        event_name="output.completed",
        payload={"output_index": 0, "content_type": "text/plain"},
    )
    execution.transition(
        machine, target, expected_revision=2, new_state=ExecutionState.SUCCEEDED
    )

    views = PostgresModelRequestViews(database_url)
    context = _administrator_context("request_status.read", workspace_id=WORKSPACE_ID)
    status = views.status(context, target)
    page, cursor = views.list_status(context, limit=1)
    assert status["state"] == "succeeded"
    assert "result" not in status
    assert page == (status,)
    assert cursor is None

    embed_status = views.status(
        _embed_context("request_status.read", workspace_id=WORKSPACE_ID), target
    )
    assert embed_status == status
    assert "result" not in embed_status

    with pytest.raises(ExecutionError) as hidden:
        views.status(
            _administrator_context(
                "request_status.read",
                service_id=OTHER_SERVICE_ID,
                workspace_id=WORKSPACE_ID,
            ),
            target,
        )
    assert hidden.value.code.value == "request_not_found"


def test_state_and_accounting_reads_apply_sql_scope_and_bounds(
    database_url: str,
) -> None:
    budget_id = "0198a080-0000-7000-8000-000000000099"
    with psycopg.connect(database_url) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """INSERT INTO router.budget_scopes (
                   id, scope_kind, service_id, workspace_id, currency, hard_limit
               ) VALUES (%s, 'workspace', %s, %s, 'USD', 100)""",
            (
                budget_id,
                SERVICE_ID,
                WORKSPACE_ID,
            ),
        )
        _record_budget_limit(connection, budget_id)
    lifecycle = PostgresLifecycleRepository(database_url)
    service = lifecycle.get_administration_state(
        _administrator_context("health.read"), SERVICE_ID
    )
    assert isinstance(service, ServiceRecord)
    assert service.service_id == SERVICE_ID
    embedded_service = lifecycle.get_administration_state(
        _embed_context("health.read"), SERVICE_ID
    )
    assert embedded_service == service
    with pytest.raises(LifecycleError):
        lifecycle.get_administration_state(
            _administrator_context("health.read", service_id=OTHER_SERVICE_ID),
            SERVICE_ID,
        )

    accounting = PostgresAccountingRepository(database_url)
    scope = Scope(SERVICE_ID, WORKSPACE_ID)
    summary = accounting.summary(
        _administrator_context("accounting.read", workspace_id=WORKSPACE_ID),
        scope,
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=1),
    )
    assert summary.logical_requests == 0
    with pytest.raises(AccountingError):
        accounting.summary(
            _administrator_context("accounting.read", service_id=OTHER_SERVICE_ID),
            scope,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=1),
        )


def test_generic_session_seam_applies_browser_and_recent_authentication(
    administration_auth: tuple[AdministratorAuthRepository, FakeIdentityService, str],
) -> None:
    repository, identity, database_url = administration_auth
    session, csrf = _bootstrap(
        repository,
        identity,
        frozenset({"assignment.manage", "health.read"}),
    )
    policy = OperationPolicy(
        "assignment.manage",
        AuthorityPath.GLOBAL_ADMINISTRATION,
        frozenset({PrincipalKind.ADMINISTRATOR}),
        ScopeKind.SERVICE,
        sensitive=True,
        mutation=True,
    )
    context = repository.authorize_session(
        session,
        request_id="authorized-write",
        now=AUTH_NOW,
        policy=policy,
        scope=Scope(SERVICE_ID),
        csrf_token=csrf,
        origin=ORIGIN,
    )
    assert context.operation == "assignment.manage"
    with pytest.raises(AdministratorAuthError) as denied:
        repository.authorize_session(
            session,
            request_id="wrong-origin",
            now=AUTH_NOW,
            policy=policy,
            scope=Scope(SERVICE_ID),
            csrf_token=csrf,
            origin="https://wrong.example.test",
        )
    assert denied.value.code == "insufficient_scope"
    with pytest.raises(AdministratorAuthError) as denied_csrf:
        repository.authorize_session(
            session,
            request_id="wrong-csrf",
            now=AUTH_NOW,
            policy=policy,
            scope=Scope(SERVICE_ID),
            csrf_token="x" * 43,
            origin=ORIGIN,
        )
    assert denied_csrf.value.code == "insufficient_scope"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE router.administrator_sessions SET recent_authenticated_at = %s",
            (AUTH_NOW - timedelta(minutes=6),),
        )
    with pytest.raises(AdministratorAuthError) as stale_authentication:
        repository.authorize_session(
            session,
            request_id="stale-recent-authentication",
            now=AUTH_NOW,
            policy=policy,
            scope=Scope(SERVICE_ID),
            csrf_token=csrf,
            origin=ORIGIN,
        )
    assert stale_authentication.value.code == "recent_auth_required"

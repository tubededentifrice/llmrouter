"""Permission, isolation, mutation, and audit tests for shared authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest
from llmrouter_backend.authority import (
    ADMINISTRATOR_OPERATIONS,
    MACHINE_OPERATIONS_BY_AUDIENCE,
    AdministratorPrincipal,
    Audience,
    AuditClass,
    AuditedMutationExecutor,
    AuditEmitter,
    AuditSafeDetail,
    AuthorityClass,
    AuthorityPath,
    BrowserWriteProof,
    IdempotencyRegistry,
    ImmutableAuditLog,
    MutationPrecondition,
    OperationPolicy,
    PermissionResult,
    Principal,
    PrincipalKind,
    SafeAuthorityError,
    SafeErrorCode,
    Scope,
    ScopeKind,
    ScopeMismatchMode,
    SensitivePermissionAuditor,
    authorize,
    require_expected_revision,
    resolve_and_lookup,
)
from llmrouter_backend.testing import ScopeSafeRecords, ScopeTestBuilder

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
SERVICE_SCOPE = Scope("service-1")
WORKSPACE_SCOPE = Scope("service-1", "workspace-1")
FINGERPRINT = "a" * 64
BROWSER_PROOF = BrowserWriteProof(
    allowed_origin="https://admin.example.test",
    request_origin="https://admin.example.test",
    session_csrf_token="session-csrf-value",  # noqa: S106
    request_csrf_token="session-csrf-value",  # noqa: S106
)


def machine_policy(
    audience: Audience,
    operation: str,
    *,
    scope_kind: ScopeKind = ScopeKind.SERVICE,
    mismatch_mode: ScopeMismatchMode = ScopeMismatchMode.HIDDEN_RECORD,
    mutation: bool = False,
) -> OperationPolicy:
    """Create one exact machine operation policy."""
    return OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=audience,
        principal_kinds=frozenset((PrincipalKind.SERVICE,)),
        scope_kind=scope_kind,
        scope_mismatch_mode=mismatch_mode,
        mutation=mutation,
    )


def administrator_policy(
    operation: str,
    *,
    scope_kind: ScopeKind = ScopeKind.GLOBAL,
    sensitive: bool = False,
    mutation: bool = False,
) -> OperationPolicy:
    """Create one global-administrator operation policy."""
    return OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        principal_kinds=frozenset((PrincipalKind.ADMINISTRATOR,)),
        scope_kind=scope_kind,
        sensitive=sensitive,
        mutation=mutation,
    )


def embed_policy(
    operation: str,
    *,
    scope_kind: ScopeKind,
    sensitive: bool = False,
) -> OperationPolicy:
    """Create one embed-session operation policy."""
    return OperationPolicy(
        operation=operation,
        authority_path=AuthorityPath.EMBED,
        principal_kinds=frozenset((PrincipalKind.EMBED,)),
        scope_kind=scope_kind,
        sensitive=sensitive,
    )


@pytest.mark.parametrize(
    ("audience", "operation"),
    [
        (Audience.DATA_PLANE, "model.create"),
        (Audience.DATA_PLANE, "embedding.create"),
        (Audience.SERVICE_MANAGEMENT, "workspace.create"),
        (Audience.HOST_BACKEND, "admin_embed.create"),
        (Audience.ACCOUNTING, "accounting.read"),
        (Audience.CONFIGURATION, "configuration.read"),
        (Audience.BUDGET_AUTHORITY, "budget_ceiling.write"),
    ],
)
def test_each_machine_audience_accepts_only_its_exact_operation(
    audience: Audience, operation: str
) -> None:
    """Each accepted machine audience and operation pair is independent."""
    builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
    principal = builder.service(operation, audience=audience)
    context = authorize(
        principal,
        machine_policy(audience, operation),
        SERVICE_SCOPE,
        request_id="request-1",
        now=NOW,
    )
    assert context.machine_audience is audience
    assert context.operation == operation


def test_machine_audience_operation_matrix_is_closed() -> None:
    """Each accepted pair works, and each cross-audience pair is rejected."""
    expected_operations = {
        Audience.DATA_PLANE: frozenset(
            {
                "model.create",
                "model.read",
                "model.cancel",
                "run.create",
                "run.read",
                "run.cancel",
                "tool.create",
                "tool.read",
                "tool.cancel",
                "attachment.create",
                "attachment.read",
                "embedding.create",
                "embedding.read",
            }
        ),
        Audience.SERVICE_MANAGEMENT: frozenset(
            {
                "workspace.create",
                "workspace.read",
                "workspace.disable",
                "workspace.restore",
                "workspace.retire",
            }
        ),
        Audience.HOST_BACKEND: frozenset({"admin_embed.create"}),
        Audience.ACCOUNTING: frozenset({"accounting.read"}),
        Audience.CONFIGURATION: frozenset(
            {
                "configuration.read",
                "configuration.write",
                "diagnostic.grant.create",
                "retention.read",
                "retention.preview",
                "retention.write",
                "budget.read",
                "budget.write",
            }
        ),
        Audience.BUDGET_AUTHORITY: frozenset(
            {"budget_ceiling.read", "budget_ceiling.write"}
        ),
    }
    assert expected_operations == MACHINE_OPERATIONS_BY_AUDIENCE
    for audience, operations in MACHINE_OPERATIONS_BY_AUDIENCE.items():
        builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
        for operation in operations:
            principal = builder.service(operation, audience=audience)
            assert authorize(
                principal,
                machine_policy(audience, operation),
                SERVICE_SCOPE,
                request_id=f"request-{audience}-{operation}",
                now=NOW,
            )
            for other_audience in set(Audience) - {audience}:
                if operation in MACHINE_OPERATIONS_BY_AUDIENCE[other_audience]:
                    continue
                with pytest.raises(ValueError, match="exact audience"):
                    replace(principal, audience=other_audience)


def test_operation_policies_reject_values_outside_the_public_contract() -> None:
    """A route cannot create authority for an open operation value."""
    with pytest.raises(ValueError, match="public contract"):
        administrator_policy("private.unreviewed")
    assert "credential.manage" in ADMINISTRATOR_OPERATIONS


@pytest.mark.parametrize(
    ("principal_name", "policy_name", "expected_code"),
    [
        ("service", "wrong_audience", SafeErrorCode.INVALID_TOKEN),
        ("service", "wrong_operation", SafeErrorCode.INSUFFICIENT_SCOPE),
        ("service", "administrator", SafeErrorCode.INVALID_TOKEN),
        ("administrator", "machine", SafeErrorCode.INVALID_TOKEN),
        ("embed", "machine", SafeErrorCode.INVALID_TOKEN),
        ("administrator", "embed", SafeErrorCode.INVALID_TOKEN),
    ],
)
def test_token_confusion_matrix_fails_closed(
    principal_name: str, policy_name: str, expected_code: SafeErrorCode
) -> None:
    """Machine, global administrator, and embed paths cannot authenticate together."""
    builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
    principals: dict[str, Principal] = {
        "service": builder.service(
            "configuration.read", audience=Audience.CONFIGURATION
        ),
        "administrator": ScopeTestBuilder(Scope(), now=NOW).administrator(
            "health.read", global_authority=True
        ),
        "embed": builder.embed("configuration.read"),
    }
    policies = {
        "wrong_audience": machine_policy(Audience.ACCOUNTING, "accounting.read"),
        "wrong_operation": machine_policy(
            Audience.CONFIGURATION, "configuration.write"
        ),
        "administrator": administrator_policy(
            "health.read", scope_kind=ScopeKind.SERVICE
        ),
        "machine": machine_policy(Audience.CONFIGURATION, "configuration.read"),
        "embed": embed_policy("configuration.read", scope_kind=ScopeKind.SERVICE),
    }
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            principals[principal_name],
            policies[policy_name],
            SERVICE_SCOPE,
            request_id="request-matrix",
            now=NOW,
        )
    assert captured.value.code is expected_code


@pytest.mark.parametrize(
    ("scope", "result"),
    [
        (Scope("service-1"), "allowed"),
        (Scope("service-1", "workspace-1"), "allowed"),
        (Scope("service-1", "workspace-2"), "denied"),
        (Scope("service-2"), "denied"),
        (Scope("service-2", "workspace-1"), "denied"),
    ],
)
def test_service_workspace_permission_matrix(scope: Scope, result: str) -> None:
    """A workspace-limited token stays in its service and workspace."""
    principal = ScopeTestBuilder(WORKSPACE_SCOPE, now=NOW).service(
        "model.read", audience=Audience.DATA_PLANE
    )
    policy = machine_policy(
        Audience.DATA_PLANE,
        "model.read",
        scope_kind=ScopeKind.SERVICE_OR_WORKSPACE,
    )
    if result == "allowed":
        assert (
            authorize(
                principal,
                policy,
                scope,
                request_id="request-scope",
                now=NOW,
            ).scope
            == scope
        )
    else:
        with pytest.raises(SafeAuthorityError) as captured:
            authorize(
                principal,
                policy,
                scope,
                request_id="request-scope",
                now=NOW,
            )
        assert captured.value.code is SafeErrorCode.NOT_FOUND


def test_omitted_and_empty_workspace_limits_have_different_results() -> None:
    """An omitted workspace limit is broad and an empty limit denies workspace data."""
    builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
    unlimited = builder.service(
        "model.read", audience=Audience.DATA_PLANE, allow_all_workspaces=True
    )
    empty = builder.service("model.read", audience=Audience.DATA_PLANE)
    policy = machine_policy(
        Audience.DATA_PLANE, "model.read", scope_kind=ScopeKind.WORKSPACE
    )
    assert authorize(
        unlimited,
        policy,
        WORKSPACE_SCOPE,
        request_id="request-unlimited",
        now=NOW,
    )
    with pytest.raises(SafeAuthorityError):
        authorize(
            empty,
            policy,
            WORKSPACE_SCOPE,
            request_id="request-empty",
            now=NOW,
        )


def test_service_administrator_workspace_set_is_exact() -> None:
    """A service grant can contain a bounded set of workspace identities."""
    principal = AdministratorPrincipal(
        issuer="https://identity.test",
        subject="operator-1",
        authority_class=AuthorityClass.SERVICE,
        operations=frozenset(("audit.read",)),
        authenticated_at=NOW - timedelta(hours=1),
        last_activity_at=NOW,
        recent_authentication_at=None,
        account_checked_at=NOW,
        idle_expires_at=NOW + timedelta(minutes=15),
        absolute_expires_at=NOW + timedelta(hours=7),
        grant_revision=2,
        allowed_service_ids=frozenset(("service-1",)),
        allowed_workspace_ids=frozenset(("workspace-1", "workspace-2")),
    )
    policy = administrator_policy("audit.read", scope_kind=ScopeKind.WORKSPACE)
    for workspace_id in ("workspace-1", "workspace-2"):
        assert authorize(
            principal,
            policy,
            Scope("service-1", workspace_id),
            request_id=f"request-{workspace_id}",
            now=NOW,
        )
    with pytest.raises(SafeAuthorityError):
        authorize(
            principal,
            policy,
            Scope("service-1", "workspace-3"),
            request_id="request-denied",
            now=NOW,
        )


def test_global_administrator_and_service_authority_do_not_expand_each_other() -> None:
    """A global grant can use the global path and a service grant cannot."""
    builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
    global_principal = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "service.manage", global_authority=True
    )
    service_principal = builder.administrator("service.manage")
    policy = administrator_policy("service.manage")
    assert authorize(
        global_principal,
        policy,
        Scope(),
        request_id="request-global",
        now=NOW,
    )
    with pytest.raises(SafeAuthorityError):
        authorize(
            service_principal,
            policy,
            Scope(),
            request_id="request-service",
            now=NOW,
        )


def test_recent_authentication_does_not_use_session_start_time() -> None:
    """A sensitive action needs the separate recent-authentication claim."""
    builder = ScopeTestBuilder(Scope(), now=NOW)
    missing = builder.administrator("credential.manage", global_authority=True)
    recent = builder.administrator(
        "credential.manage",
        global_authority=True,
        recent_authentication_at=NOW - timedelta(minutes=5),
    )
    policy = administrator_policy("credential.manage", sensitive=True)
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            missing,
            policy,
            Scope(),
            request_id="request-old",
            now=NOW,
        )
    assert captured.value.code is SafeErrorCode.RECENT_AUTH_REQUIRED
    context = authorize(
        recent,
        policy,
        Scope(),
        request_id="request-recent",
        now=NOW,
    )
    assert context.recent_authentication_at == NOW - timedelta(minutes=5)


def test_future_account_check_fails_authentication() -> None:
    """An account-state check cannot be more than the clock-skew limit in the future."""
    builder = ScopeTestBuilder(Scope(), now=NOW)
    principal = builder.administrator("service.manage", global_authority=True)
    future = replace(principal, account_checked_at=NOW + timedelta(seconds=31))
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            future,
            administrator_policy("service.manage"),
            Scope(),
            request_id="request-future-check",
            now=NOW,
        )
    assert captured.value.code is SafeErrorCode.INVALID_TOKEN


def test_future_administrator_activity_fails_authentication() -> None:
    """A future last-activity value cannot extend an administrator session."""
    builder = ScopeTestBuilder(Scope(), now=NOW)
    principal = builder.administrator("health.read", global_authority=True)
    future = replace(principal, last_activity_at=NOW + timedelta(seconds=1))
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            future,
            administrator_policy("health.read"),
            Scope(),
            request_id="request-future-activity",
            now=NOW,
        )
    assert captured.value.code is SafeErrorCode.INVALID_TOKEN


def test_administrator_session_lifetime_bounds_are_closed() -> None:
    """Idle and absolute session expiry limits cannot be extended."""
    principal = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "health.read", global_authority=True
    )
    with pytest.raises(ValueError, match="15 minutes"):
        replace(
            principal,
            idle_expires_at=principal.last_activity_at + timedelta(minutes=16),
        )
    with pytest.raises(ValueError, match="eight hours"):
        replace(
            principal,
            absolute_expires_at=principal.authenticated_at
            + timedelta(hours=8, seconds=1),
        )


def test_administrator_and_embed_sessions_expire_at_the_exact_boundary() -> None:
    """An administrator or embed session is invalid at its expiry time."""
    administrator = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "health.read", global_authority=True
    )
    administrator = replace(
        administrator,
        last_activity_at=NOW - timedelta(seconds=1),
        idle_expires_at=NOW,
    )
    with pytest.raises(SafeAuthorityError) as administrator_error:
        authorize(
            administrator,
            administrator_policy("health.read"),
            Scope(),
            request_id="request-admin-expiry",
            now=NOW,
        )
    assert administrator_error.value.code is SafeErrorCode.INVALID_TOKEN

    embed = ScopeTestBuilder(SERVICE_SCOPE, now=NOW).embed("configuration.read")
    embed = replace(
        embed,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW,
    )
    with pytest.raises(SafeAuthorityError) as embed_error:
        authorize(
            embed,
            embed_policy("configuration.read", scope_kind=ScopeKind.SERVICE),
            SERVICE_SCOPE,
            request_id="request-embed-expiry",
            now=NOW,
        )
    assert embed_error.value.code is SafeErrorCode.INVALID_TOKEN


@pytest.mark.parametrize(
    "proof",
    [
        None,
        replace(BROWSER_PROOF, request_origin="https://other.example.test"),
        replace(BROWSER_PROOF, request_csrf_token="other-csrf-value"),  # noqa: S106
    ],
)
def test_administrator_write_requires_exact_browser_proof(
    proof: BrowserWriteProof | None,
) -> None:
    """An administrator write needs matching Origin and CSRF values."""
    principal = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "grant.manage", global_authority=True, recent_authentication_at=NOW
    )
    policy = administrator_policy("grant.manage", mutation=True, sensitive=True)
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            principal,
            policy,
            Scope(),
            request_id="request-browser-write",
            now=NOW,
            browser_write_proof=proof,
        )
    assert captured.value.code is SafeErrorCode.INSUFFICIENT_SCOPE
    assert authorize(
        principal,
        policy,
        Scope(),
        request_id="request-browser-write-ok",
        now=NOW,
        browser_write_proof=BROWSER_PROOF,
    )


def test_sensitive_denial_emits_one_safe_audit_event() -> None:
    """A denied sensitive action writes its safe permission result before failure."""
    builder = ScopeTestBuilder(Scope(), now=NOW)
    principal = builder.administrator("credential.manage", global_authority=True)
    log = ImmutableAuditLog()
    auditor = SensitivePermissionAuditor(AuditEmitter(log))
    with pytest.raises(SafeAuthorityError) as captured:
        auditor.authorize(
            principal,
            administrator_policy("credential.manage", sensitive=True),
            Scope(),
            request_id="request-sensitive-denied",
            now=NOW,
            event_id="denial-event-1",
            audit_class=AuditClass.SECURITY,
        )
    assert captured.value.code is SafeErrorCode.RECENT_AUTH_REQUIRED
    assert len(log.events) == 1
    event = log.events[0]
    assert event.permission_result is PermissionResult.DENIED
    assert event.safe_detail == AuditSafeDetail(safe_error_code="recent_auth_required")
    assert event.authority_class is AuthorityClass.GLOBAL_ADMINISTRATOR


def test_sensitive_permission_emits_one_permitted_audit_event() -> None:
    """A permitted sensitive action writes its decision audit event."""
    principal = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "content.read",
        global_authority=True,
        recent_authentication_at=NOW,
    )
    log = ImmutableAuditLog()
    context = SensitivePermissionAuditor(AuditEmitter(log)).authorize(
        principal,
        administrator_policy("content.read", sensitive=True),
        Scope(),
        request_id="request-sensitive-permitted",
        now=NOW,
        event_id="permission-event-1",
        audit_class=AuditClass.SECURITY,
    )
    assert context.operation == "content.read"
    assert len(log.events) == 1
    assert log.events[0].permission_result is PermissionResult.PERMITTED


def test_sensitive_mutation_emits_only_its_mutation_audit_event() -> None:
    """A permitted sensitive mutation does not create a second decision event."""
    principal = ScopeTestBuilder(Scope(), now=NOW).administrator(
        "grant.manage",
        global_authority=True,
        recent_authentication_at=NOW,
    )
    policy = administrator_policy("grant.manage", mutation=True, sensitive=True)
    log = ImmutableAuditLog()
    emitter = AuditEmitter(log)
    context = SensitivePermissionAuditor(emitter).authorize(
        principal,
        policy,
        Scope(),
        request_id="request-sensitive-mutation",
        now=NOW,
        event_id="unused-permission-event",
        audit_class=AuditClass.SECURITY,
        browser_write_proof=BROWSER_PROOF,
    )
    assert log.events == ()

    result = AuditedMutationExecutor(IdempotencyRegistry(), emitter).execute(
        context,
        MutationPrecondition("revision-1", "sensitive-mutation-key", FINGERPRINT),
        lambda: "grant-updated",
        current_revision="revision-1",
        event_id="mutation-event-1",
        audit_class=AuditClass.SECURITY,
    )
    assert result.value == "grant-updated"
    assert len(log.events) == 1
    assert log.events[0].event_id == "mutation-event-1"


def test_export_status_read_does_not_require_recent_authentication() -> None:
    """An export status read and an export mutation have separate sensitivity."""
    assert administrator_policy("export.create")
    with pytest.raises(ValueError, match="recent authentication"):
        administrator_policy("export.create", mutation=True)
    assert administrator_policy("export.create", mutation=True, sensitive=True)


def test_embed_service_session_has_no_workspace_data_authority() -> None:
    """An embed session with no workspace can use only service-level operations."""
    principal = ScopeTestBuilder(SERVICE_SCOPE, now=NOW).embed("configuration.read")
    assert authorize(
        principal,
        embed_policy("configuration.read", scope_kind=ScopeKind.SERVICE),
        SERVICE_SCOPE,
        request_id="request-service-view",
        now=NOW,
    )
    with pytest.raises(SafeAuthorityError):
        authorize(
            principal,
            embed_policy("configuration.read", scope_kind=ScopeKind.WORKSPACE),
            WORKSPACE_SCOPE,
            request_id="request-workspace-view",
            now=NOW,
        )


def test_embed_session_lifetime_and_service_level_data_are_bounded() -> None:
    """An embed session lasts five minutes and cannot broaden omitted workspace data."""
    builder = ScopeTestBuilder(SERVICE_SCOPE, now=NOW)
    service_level = builder.embed("configuration.read")
    with pytest.raises(ValueError, match="five minutes"):
        replace(service_level, expires_at=NOW + timedelta(minutes=5, seconds=1))
    data_principal = builder.embed("accounting.read")
    with pytest.raises(SafeAuthorityError):
        authorize(
            data_principal,
            embed_policy("accounting.read", scope_kind=ScopeKind.SERVICE),
            SERVICE_SCOPE,
            request_id="request-service-data",
            now=NOW,
        )


def test_hidden_record_scope_is_resolved_before_lookup() -> None:
    """A scope denial and an absent record have the same safe result."""
    principal = ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
        "workspace.read",
        audience=Audience.SERVICE_MANAGEMENT,
        allow_all_workspaces=True,
    )
    policy = machine_policy(
        Audience.SERVICE_MANAGEMENT,
        "workspace.read",
        scope_kind=ScopeKind.WORKSPACE,
    )
    called = False

    def forbidden_lookup() -> str | None:
        nonlocal called
        called = True
        return "private-record"

    with pytest.raises(SafeAuthorityError) as denied:
        resolve_and_lookup(
            principal,
            policy,
            Scope("service-2", "workspace-private"),
            forbidden_lookup,
            request_id="request-hidden",
            now=NOW,
        )
    assert not called

    with pytest.raises(SafeAuthorityError) as absent:
        resolve_and_lookup(
            principal,
            policy,
            Scope("service-1", "workspace-missing"),
            lambda: None,
            request_id="request-hidden",
            now=NOW,
        )
    assert denied.value.to_envelope() == absent.value.to_envelope()


@pytest.mark.parametrize(
    ("scope", "expected_code"),
    [
        (Scope("service-2"), SafeErrorCode.SERVICE_SCOPE_MISMATCH),
        (
            Scope("service-2", "workspace-2"),
            SafeErrorCode.SERVICE_SCOPE_MISMATCH,
        ),
        (
            Scope("service-1", "workspace-2"),
            SafeErrorCode.WORKSPACE_SCOPE_MISMATCH,
        ),
    ],
)
def test_explicit_scope_mismatch_uses_formal_error(
    scope: Scope, expected_code: SafeErrorCode
) -> None:
    """A prelookup scope assertion can return the formal mismatch code."""
    principal = ScopeTestBuilder(WORKSPACE_SCOPE, now=NOW).service(
        "model.create", audience=Audience.DATA_PLANE
    )
    policy = machine_policy(
        Audience.DATA_PLANE,
        "model.create",
        scope_kind=ScopeKind.SERVICE_OR_WORKSPACE,
        mismatch_mode=ScopeMismatchMode.EXPLICIT,
    )
    with pytest.raises(SafeAuthorityError) as captured:
        authorize(
            principal,
            policy,
            scope,
            request_id="request-mismatch",
            now=NOW,
        )
    assert captured.value.code is expected_code


def test_safe_error_does_not_include_private_lookup_data() -> None:
    """The public error contains only the closed safe fields."""
    principal = ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
        "model.read", audience=Audience.DATA_PLANE
    )
    with pytest.raises(SafeAuthorityError) as captured:
        resolve_and_lookup(
            principal,
            machine_policy(Audience.DATA_PLANE, "model.read"),
            Scope("private-service"),
            lambda: {"secret": "do-not-report"},
            request_id="request-safe",
            now=NOW,
        )
    assert captured.value.to_envelope() == {
        "error": {
            "code": "not_found",
            "message": "The requested record was not found.",
            "retryable": False,
            "request_id": "request-safe",
        }
    }


def test_expected_revision_rejects_a_stale_write() -> None:
    """A stale expected revision fails before mutation work starts."""
    precondition = MutationPrecondition("revision-1", "idempotency-key-1", FINGERPRINT)
    require_expected_revision(precondition, "revision-1", request_id="request-current")
    with pytest.raises(SafeAuthorityError) as captured:
        require_expected_revision(
            precondition, "revision-2", request_id="request-stale"
        )
    assert captured.value.code is SafeErrorCode.STATE_REVISION_CONFLICT


def test_idempotency_is_atomic_and_bound_to_actor_authority() -> None:
    """Concurrent equal work runs once and a separate actor has its own key."""
    registry = IdempotencyRegistry()
    policy = machine_policy(
        Audience.CONFIGURATION,
        "budget.write",
        mutation=True,
    )
    service_context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "budget.write", audience=Audience.CONFIGURATION
        ),
        policy,
        SERVICE_SCOPE,
        request_id="request-service",
        now=NOW,
    )
    administrator_context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).administrator("budget.write"),
        administrator_policy(
            "budget.write", scope_kind=ScopeKind.SERVICE, mutation=True
        ),
        SERVICE_SCOPE,
        request_id="request-administrator",
        now=NOW,
        browser_write_proof=BROWSER_PROOF,
    )
    precondition = MutationPrecondition(
        "revision-1", "same-key-for-actors", FINGERPRINT
    )
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        return "service-result"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: registry.execute(service_context, precondition, operation),
                range(16),
            )
        )
    assert calls == 1
    assert sum(not result.replayed for result in results) == 1
    admin_result = registry.execute(
        administrator_context, precondition, lambda: "administrator-result"
    )
    assert admin_result.value == "administrator-result"
    assert not admin_result.replayed


def test_changed_idempotency_replay_fails() -> None:
    """A key cannot bind to a second mutation fingerprint."""
    context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "workspace.disable", audience=Audience.SERVICE_MANAGEMENT
        ),
        machine_policy(Audience.SERVICE_MANAGEMENT, "workspace.disable", mutation=True),
        SERVICE_SCOPE,
        request_id="request-conflict",
        now=NOW,
    )
    registry = IdempotencyRegistry()
    first = MutationPrecondition("revision-1", "idempotency-key-1", "a" * 64)
    changed = MutationPrecondition("revision-1", "idempotency-key-1", "b" * 64)
    registry.execute(context, first, lambda: "first")
    with pytest.raises(SafeAuthorityError) as captured:
        registry.execute(context, changed, lambda: "unsafe")
    assert captured.value.code is SafeErrorCode.IDEMPOTENCY_CONFLICT


def test_audited_mutation_emits_one_event_for_concurrent_replays() -> None:
    """One successful protected mutation has one immutable audit event."""
    context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "workspace.disable", audience=Audience.SERVICE_MANAGEMENT
        ),
        machine_policy(Audience.SERVICE_MANAGEMENT, "workspace.disable", mutation=True),
        SERVICE_SCOPE,
        request_id="request-audit",
        now=NOW,
    )
    audit_log = ImmutableAuditLog()
    executor = AuditedMutationExecutor(IdempotencyRegistry(), AuditEmitter(audit_log))
    precondition = MutationPrecondition("revision-1", "idempotency-key-1", FINGERPRINT)
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        return "disabled"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: executor.execute(
                    context,
                    precondition,
                    operation,
                    current_revision="revision-1",
                    event_id="event-1",
                    audit_class=AuditClass.SECURITY,
                    detail=AuditSafeDetail(
                        resource_type="workspace", resource_id="workspace-1"
                    ),
                ),
                range(16),
            )
        )
    assert calls == 1
    assert len(audit_log.events) == 1
    assert all(result.event is results[0].event for result in results)
    assert sum(not result.replayed for result in results) == 1
    with pytest.raises(FrozenInstanceError):
        results[0].event.action = "changed"  # type: ignore[misc]


def test_audit_identity_is_checked_before_mutation_work() -> None:
    """An audit collision fails before the mutation callback can change state."""
    context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "workspace.disable", audience=Audience.SERVICE_MANAGEMENT
        ),
        machine_policy(Audience.SERVICE_MANAGEMENT, "workspace.disable", mutation=True),
        SERVICE_SCOPE,
        request_id="request-audit-collision",
        now=NOW,
    )
    log = ImmutableAuditLog()
    emitter = AuditEmitter(log)
    emitter.emit(
        context,
        event_id="event-collision",
        audit_class=AuditClass.SECURITY,
        permission_result=PermissionResult.PERMITTED,
    )
    executor = AuditedMutationExecutor(IdempotencyRegistry(), emitter)
    calls = 0

    def unsafe_operation() -> str:
        nonlocal calls
        calls += 1
        return "unsafe"

    with pytest.raises(SafeAuthorityError):
        executor.execute(
            context,
            MutationPrecondition("revision-1", "idempotency-collision", FINGERPRINT),
            unsafe_operation,
            current_revision="revision-1",
            event_id="event-collision",
            audit_class=AuditClass.SECURITY,
        )
    assert calls == 0
    assert len(log.events) == 1


def test_equal_replay_precedes_revision_check_and_new_work_does_not() -> None:
    """An equal replay is stable, but new work must match the current revision."""
    context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "workspace.disable", audience=Audience.SERVICE_MANAGEMENT
        ),
        machine_policy(Audience.SERVICE_MANAGEMENT, "workspace.disable", mutation=True),
        SERVICE_SCOPE,
        request_id="request-revision-order",
        now=NOW,
    )
    log = ImmutableAuditLog()
    executor = AuditedMutationExecutor(IdempotencyRegistry(), AuditEmitter(log))
    equal = MutationPrecondition("revision-1", "idempotency-equal-1", FINGERPRINT)
    first = executor.execute(
        context,
        equal,
        lambda: "revision-2",
        current_revision="revision-1",
        event_id="event-equal",
        audit_class=AuditClass.SECURITY,
    )
    replay = executor.execute(
        context,
        equal,
        lambda: "unsafe",
        current_revision="revision-2",
        event_id="event-equal",
        audit_class=AuditClass.SECURITY,
    )
    assert first.value == replay.value == "revision-2"
    assert replay.replayed

    new = MutationPrecondition("revision-1", "idempotency-new-key", FINGERPRINT)
    with pytest.raises(SafeAuthorityError) as captured:
        executor.execute(
            context,
            new,
            lambda: "unsafe",
            current_revision="revision-2",
            event_id="event-new",
            audit_class=AuditClass.SECURITY,
        )
    assert captured.value.code is SafeErrorCode.STATE_REVISION_CONFLICT
    assert [event.event_id for event in log.events] == ["event-equal"]


def test_audit_duplicate_identity_rejects_changed_content() -> None:
    """An equal audit replay is safe and changed event content fails."""
    context = authorize(
        ScopeTestBuilder(SERVICE_SCOPE, now=NOW).service(
            "workspace.read", audience=Audience.SERVICE_MANAGEMENT
        ),
        machine_policy(Audience.SERVICE_MANAGEMENT, "workspace.read"),
        SERVICE_SCOPE,
        request_id="request-event",
        now=NOW,
    )
    log = ImmutableAuditLog()
    emitter = AuditEmitter(log)
    first = emitter.emit(
        context,
        event_id="event-1",
        audit_class=AuditClass.SECURITY,
        permission_result=PermissionResult.PERMITTED,
    )
    assert log.append_once(first, request_id="request-event") is False
    changed = type(first)(
        event_id=first.event_id,
        audit_class=first.audit_class,
        actor_kind=first.actor_kind,
        actor_id=first.actor_id,
        authority_class=first.authority_class,
        scope=first.scope,
        action="workspace.disable",
        permission_result=first.permission_result,
        occurred_at=first.occurred_at,
    )
    with pytest.raises(SafeAuthorityError):
        log.append_once(changed, request_id="request-event")


def test_scope_safe_record_builder_rejects_cross_scope_context() -> None:
    """The test record builder cannot silently read through another scope."""
    builder = ScopeTestBuilder(WORKSPACE_SCOPE, now=NOW)
    context = authorize(
        builder.service("model.read", audience=Audience.DATA_PLANE),
        machine_policy(
            Audience.DATA_PLANE, "model.read", scope_kind=ScopeKind.WORKSPACE
        ),
        WORKSPACE_SCOPE,
        request_id="request-record",
        now=NOW,
    )
    records: ScopeSafeRecords[str] = ScopeSafeRecords(WORKSPACE_SCOPE)
    records.add("record-1", "value")
    assert records.lookup(context, "record-1") == "value"
    wrong_context = type(context)(
        request_id=context.request_id,
        actor_kind=context.actor_kind,
        actor_id=context.actor_id,
        authority_class=context.authority_class,
        authority_path=context.authority_path,
        machine_audience=context.machine_audience,
        operation=context.operation,
        scope=Scope("service-1", "workspace-2"),
        authorized_at=context.authorized_at,
        recent_authentication_at=context.recent_authentication_at,
        mutation=context.mutation,
    )
    with pytest.raises(ValueError, match="does not match"):
        records.lookup(wrong_context, "record-1")

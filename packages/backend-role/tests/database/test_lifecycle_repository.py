"""Real PostgreSQL tests for service and workspace lifecycle operations."""

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    BudgetScopeKind,
    BudgetTarget,
    PostgresBudgetRepository,
    ResetPeriod,
)
from llmrouter_backend.database import migrate
from llmrouter_backend.lifecycle import (
    LifecycleError,
    LifecycleErrorCode,
    LifecycleState,
    PostgresLifecycleRepository,
    ServiceAction,
    WorkspaceAction,
)
from llmrouter_backend.machine_identity import (
    BootstrapScope,
    MachineCredentialRepository,
    MachineIdentityError,
)

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
CREATE_KEY = "workspace-create-key-0001"
DISABLE_KEY = "workspace-disable-key-001"


def _global_context(
    operation: str = "service.manage", *, request_id: str = "admin-request"
) -> RequestContext:
    return RequestContext(
        request_id=request_id,
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="issuer:administrator",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation=operation,
        scope=Scope(),
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=True,
    )


def _service_context(
    service_id: str,
    operation: str,
    *,
    workspace_id: str | None = None,
    request_id: str = "service-request",
) -> RequestContext:
    return RequestContext(
        request_id=request_id,
        actor_kind=PrincipalKind.SERVICE,
        actor_id=service_id,
        authority_class=AuthorityClass.SERVICE,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=Audience.SERVICE_MANAGEMENT,
        operation=operation,
        scope=Scope(service_id, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=operation != "workspace.read",
    )


@pytest.fixture
def repository(database_url: str) -> PostgresLifecycleRepository:
    """Apply all migrations and return the lifecycle authority."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
    return PostgresLifecycleRepository(database_url)


def _create_service(
    repository: PostgresLifecycleRepository,
    *,
    key: str = "service-create-key-00001",
    name: str = "Service A",
    parent: str | None = None,
) -> str:
    return repository.create_service(
        _global_context(),
        idempotency_key=key,
        display_name=name,
        parent_service_id=parent,
    ).value.service_id


def _create_workspace(
    repository: PostgresLifecycleRepository,
    service_id: str,
) -> str:
    return repository.create_workspace(
        _service_context(service_id, "workspace.create"),
        idempotency_key=CREATE_KEY,
        caller_reference="caller-workspace-a",
        display_name="Workspace A",
    ).value.workspace_id


def test_concurrent_equal_service_create_has_one_result_and_audit(
    database_url: str,
) -> None:
    """Serialize equal create requests into one service and one audit event."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)

    def create() -> str:
        repository = PostgresLifecycleRepository(database_url)
        return repository.create_service(
            _global_context(request_id="concurrent-create"),
            idempotency_key="concurrent-service-key-1",
            display_name="Concurrent service",
        ).value.service_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        service_ids = list(executor.map(lambda _index: create(), range(2)))
    assert service_ids[0] == service_ids[1]
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.services WHERE id = %s", (service_ids[0],)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM router.audit_events WHERE service_id = %s",
            (service_ids[0],),
        ).fetchone() == (1,)


def test_service_registry_is_atomic_and_keeps_lifecycle_only_rows_visible(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Coordinate new bootstrap state and report older missing state safely."""
    lifecycle_only = _create_service(repository, key="lifecycle-only-key-01")
    credentials = MachineCredentialRepository(
        database_url,
        issuer="https://router.example.test",
        digest_keys={"test-v1": b"d" * 32},
        current_digest_key_id="test-v1",
    )
    scope = BootstrapScope(
        frozenset({Audience.DATA_PLANE}), frozenset({"model.create"})
    )
    created, bootstrap = repository.create_service_with_bootstrap(
        _global_context(),
        _global_context("credential.manage"),
        credentials,
        idempotency_key="atomic-service-key-0001",
        display_name="Atomic service",
        parent_service_id=None,
        bootstrap_scope=scope,
        now=NOW,
    )
    replay, replay_bootstrap = repository.create_service_with_bootstrap(
        _global_context(),
        _global_context("credential.manage"),
        credentials,
        idempotency_key="atomic-service-key-0001",
        display_name="Atomic service",
        parent_service_id=None,
        bootstrap_scope=scope,
        now=NOW,
    )

    assert bootstrap is not None
    assert bootstrap.generation == 1
    assert replay.replayed
    assert replay.value == created.value
    assert replay_bootstrap is None
    records = repository.list_service_administration(_global_context())
    assert [record.service_id for record in records] == sorted(
        [lifecycle_only, created.value.service_id]
    )
    missing = next(record for record in records if record.service_id == lifecycle_only)
    ready = repository.get_service_administration(
        _global_context(), created.value.service_id
    )
    assert missing.bootstrap_state == "missing"
    assert missing.credential_generation is None
    assert missing.bootstrap_audiences is None
    assert ready.bootstrap_state == "ready"
    assert ready.credential_generation == 1
    assert ready.bootstrap_audiences == ("data_plane",)
    assert ready.bootstrap_operations == ("model.create",)
    credentials.revoke_generation(
        _global_context("credential.manage"),
        created.value.service_id,
        1,
        now=NOW,
    )
    revoked = repository.get_service_administration(
        _global_context(), created.value.service_id
    )
    assert revoked.bootstrap_state == "revoked"

    with pytest.raises(MachineIdentityError):
        repository.create_service_with_bootstrap(
            _global_context(),
            _global_context("service.manage"),
            credentials,
            idempotency_key="atomic-service-key-0002",
            display_name="Must roll back",
            parent_service_id=None,
            bootstrap_scope=scope,
            now=NOW,
        )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.services WHERE display_name = %s",
            ("Must roll back",),
        ).fetchone() == (0,)


def test_service_metadata_change_is_revision_safe_and_rejects_cycles(
    repository: PostgresLifecycleRepository,
) -> None:
    """Rename and reparent atomically with stale and cycle protection."""
    parent = _create_service(repository, key="metadata-parent-key-01", name="Parent")
    child = _create_service(repository, key="metadata-child-key-001", name="Child")
    changed = repository.change_service_metadata(
        _global_context("service_parent.manage"),
        child,
        expected_revision="1",
        display_name="Renamed child",
        new_parent_service_id=parent,
        reason="Group the service",
    )
    assert changed.value.display_name == "Renamed child"
    assert changed.value.parent_service_id == parent
    assert changed.value.revision == "2"
    with pytest.raises(LifecycleError) as stale:
        repository.change_service_metadata(
            _global_context("service_parent.manage"),
            child,
            expected_revision="1",
            display_name="Stale name",
            new_parent_service_id=None,
            reason="Use a stale revision",
        )
    assert stale.value.code is LifecycleErrorCode.STATE_REVISION_CONFLICT
    with pytest.raises(LifecycleError) as cycle:
        repository.change_service_metadata(
            _global_context("service_parent.manage"),
            parent,
            expected_revision="1",
            display_name="Parent",
            new_parent_service_id=child,
            reason="Create a cycle",
        )
    assert cycle.value.code is LifecycleErrorCode.INVALID_REQUEST


def test_service_lifecycle_has_exact_revisions_receipts_and_retirement(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Apply the complete service state machine and retain its identity."""
    parent = _create_service(repository, name="Parent")
    created = repository.create_service(
        _global_context(),
        idempotency_key="service-create-key-00002",
        display_name="Child",
    )
    child = created.value.service_id
    replay = repository.create_service(
        _global_context(),
        idempotency_key="service-create-key-00002",
        display_name="Child",
    )
    assert replay.replayed
    assert replay.value == created.value
    with pytest.raises(LifecycleError) as changed_create:
        repository.create_service(
            _global_context(),
            idempotency_key="service-create-key-00002",
            display_name="Changed child",
        )
    assert changed_create.value.code is LifecycleErrorCode.IDEMPOTENCY_CONFLICT

    parented = repository.change_service_parent(
        _global_context("service_parent.manage"),
        child,
        expected_revision="1",
        new_parent_service_id=parent,
        reason="Set the parent.",
    )
    assert parented.changed
    assert parented.value.revision == "2"
    assert repository.get_service(_global_context(), child) == parented.value

    disabled = repository.change_service_state(
        _global_context(),
        child,
        ServiceAction.DISABLE,
        expected_revision="2",
        idempotency_key="service-disable-key-0001",
        reason="Pause the service.",
    )
    assert disabled.changed
    assert disabled.value.revision == "3"
    disabled_replay = repository.change_service_state(
        _global_context(),
        child,
        ServiceAction.DISABLE,
        expected_revision="2",
        idempotency_key="service-disable-key-0001",
        reason="Pause the service.",
    )
    assert disabled_replay.replayed
    assert disabled_replay.value == disabled.value
    no_change = repository.change_service_state(
        _global_context(),
        child,
        ServiceAction.DISABLE,
        expected_revision="3",
        idempotency_key="service-disable-key-0002",
        reason="Keep the service paused.",
    )
    assert not no_change.changed
    assert no_change.value.revision == "3"

    restored = repository.change_service_state(
        _global_context(),
        child,
        ServiceAction.RESTORE,
        expected_revision="3",
        idempotency_key="service-restore-key-0001",
        reason="Resume the service.",
    )
    assert restored.value.state is LifecycleState.ACTIVE
    assert restored.value.revision == "4"
    retired = repository.change_service_state(
        _global_context(),
        child,
        ServiceAction.RETIRE,
        expected_revision="4",
        idempotency_key="service-retire-key-0001",
        reason="Retire the service.",
    )
    assert retired.value.state is LifecycleState.RETIRED
    assert retired.value.revision == "5"
    with pytest.raises(LifecycleError) as terminal:
        repository.change_service_state(
            _global_context(),
            child,
            ServiceAction.RESTORE,
            expected_revision="5",
            idempotency_key="service-restore-key-0002",
            reason="Invalid restore.",
        )
    assert terminal.value.code is LifecycleErrorCode.TERMINAL_STATE

    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.audit_events WHERE service_id = %s",
            (child,),
        ).fetchone() == (6,)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute("DELETE FROM router.services WHERE id = %s", (child,))


def test_concurrent_equal_workspace_create_has_one_result_and_audit(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Serialize equal workspace creates into one identity and audit event."""
    service_id = _create_service(repository)

    def create() -> str:
        result = PostgresLifecycleRepository(database_url).create_workspace(
            _service_context(service_id, "workspace.create"),
            idempotency_key=CREATE_KEY,
            caller_reference="caller-workspace-a",
            display_name="Workspace A",
        )
        return result.value.workspace_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        workspace_ids = list(executor.map(lambda _index: create(), range(2)))
    assert workspace_ids[0] == workspace_ids[1]
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.workspaces WHERE id = %s",
            (workspace_ids[0],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM router.audit_events WHERE workspace_id = %s",
            (workspace_ids[0],),
        ).fetchone() == (1,)


def test_workspace_create_replay_and_conflict_matrix(
    repository: PostgresLifecycleRepository,
) -> None:
    """Return one stable create receipt and reject changed identity reuse."""
    service_id = _create_service(repository)
    context = _service_context(service_id, "workspace.create")
    first = repository.create_workspace(
        context,
        idempotency_key=CREATE_KEY,
        caller_reference="caller-workspace-a",
        display_name="Workspace A",
    )
    replay = repository.create_workspace(
        context,
        idempotency_key=CREATE_KEY,
        caller_reference="caller-workspace-a",
        display_name="Workspace A",
    )
    assert replay.replayed
    assert replay.value == first.value
    caller_reference_replay = repository.create_workspace(
        context,
        idempotency_key="workspace-create-key-0002",
        caller_reference="caller-workspace-a",
        display_name="Workspace A",
    )
    assert caller_reference_replay.replayed
    assert caller_reference_replay.value == first.value

    with pytest.raises(LifecycleError) as changed_key:
        repository.create_workspace(
            context,
            idempotency_key=CREATE_KEY,
            caller_reference="caller-workspace-a",
            display_name="Changed",
        )
    assert changed_key.value.code is LifecycleErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(LifecycleError) as changed_reference:
        repository.create_workspace(
            context,
            idempotency_key="workspace-create-key-0003",
            caller_reference="caller-workspace-a",
            display_name="Changed",
        )
    assert changed_reference.value.code is LifecycleErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize(
    "forgery",
    ["actor", "audience"],
)
def test_workspace_context_rejects_a_forged_actor_or_audience(
    repository: PostgresLifecycleRepository,
    forgery: str,
) -> None:
    """Reject a normalized context that does not bind actor and audience."""
    service_id = _create_service(repository)
    valid = _service_context(service_id, "workspace.create")
    context = (
        replace(valid, actor_id="another-service")
        if forgery == "actor"
        else replace(valid, machine_audience=Audience.DATA_PLANE)
    )
    with pytest.raises(LifecycleError) as denied:
        repository.create_workspace(
            context,
            idempotency_key=CREATE_KEY,
            caller_reference="caller-workspace-a",
            display_name="Workspace A",
        )
    assert denied.value.code is LifecycleErrorCode.INSUFFICIENT_SCOPE


def test_malformed_lifecycle_identities_return_safe_closed_results(
    repository: PostgresLifecycleRepository,
) -> None:
    """Do not expose PostgreSQL UUID errors for opaque path identities."""
    with pytest.raises(LifecycleError) as service_read:
        repository.get_service(_global_context(), "not-a-service-uuid")
    assert service_read.value.code is LifecycleErrorCode.NOT_FOUND
    with pytest.raises(LifecycleError) as service_change:
        repository.change_service_state(
            _global_context(),
            "not-a-service-uuid",
            ServiceAction.DISABLE,
            expected_revision="1",
            idempotency_key="service-disable-key-0001",
            reason="Invalid target.",
        )
    assert service_change.value.code is LifecycleErrorCode.NOT_FOUND
    with pytest.raises(LifecycleError) as parent_create:
        repository.create_service(
            _global_context(),
            idempotency_key="service-create-key-00001",
            display_name="Invalid child",
            parent_service_id="not-a-parent-uuid",
        )
    assert parent_create.value.code is LifecycleErrorCode.NOT_FOUND

    service_id = _create_service(repository)
    with pytest.raises(LifecycleError) as parent_change:
        repository.change_service_parent(
            _global_context("service_parent.manage"),
            service_id,
            expected_revision="1",
            new_parent_service_id="not-a-parent-uuid",
            reason="Invalid parent.",
        )
    assert parent_change.value.code is LifecycleErrorCode.NOT_FOUND
    with pytest.raises(LifecycleError) as malformed_service_parent_change:
        repository.change_service_parent(
            _global_context("service_parent.manage"),
            "not-a-service-uuid",
            expected_revision="1",
            new_parent_service_id=None,
            reason="Invalid service.",
        )
    assert malformed_service_parent_change.value.code is LifecycleErrorCode.NOT_FOUND
    with pytest.raises(LifecycleError) as workspace_create:
        repository.create_workspace(
            _service_context("not-a-service-uuid", "workspace.create"),
            idempotency_key=CREATE_KEY,
            caller_reference="caller-workspace-a",
            display_name="Invalid workspace",
        )
    assert workspace_create.value.code is LifecycleErrorCode.WORKSPACE_NOT_FOUND
    with pytest.raises(LifecycleError) as workspace_read:
        repository.get_workspace(
            _service_context(
                service_id,
                "workspace.read",
                workspace_id="not-a-workspace-uuid",
            ),
            "not-a-workspace-uuid",
        )
    assert workspace_read.value.code is LifecycleErrorCode.WORKSPACE_NOT_FOUND
    with pytest.raises(LifecycleError) as workspace_change:
        repository.change_workspace_state(
            _service_context(
                service_id,
                "workspace.disable",
                workspace_id="not-a-workspace-uuid",
            ),
            "not-a-workspace-uuid",
            WorkspaceAction.DISABLE,
            expected_revision="1",
            idempotency_key=DISABLE_KEY,
            reason="Invalid target.",
        )
    assert workspace_change.value.code is LifecycleErrorCode.WORKSPACE_NOT_FOUND
    assert not repository.admission_is_allowed("not-a-service-uuid")
    assert not repository.admission_is_allowed(service_id, "not-a-workspace-uuid")


def test_workspace_state_machine_replay_no_change_and_terminal(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Keep exact revisions, stable receipts, and one audit for each new key."""
    service_id = _create_service(repository)
    workspace_id = _create_workspace(repository, service_id)
    disabled = repository.change_workspace_state(
        _service_context(service_id, "workspace.disable", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.DISABLE,
        expected_revision="1",
        idempotency_key=DISABLE_KEY,
        reason="Pause work.",
    )
    assert disabled.changed
    assert disabled.value.state_revision == "2"
    replay = repository.change_workspace_state(
        _service_context(service_id, "workspace.disable", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.DISABLE,
        expected_revision="1",
        idempotency_key=DISABLE_KEY,
        reason="Pause work.",
    )
    assert replay.replayed
    assert replay.value == disabled.value

    no_change = repository.change_workspace_state(
        _service_context(service_id, "workspace.disable", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.DISABLE,
        expected_revision="2",
        idempotency_key="workspace-disable-key-002",
        reason="Keep work paused.",
    )
    assert not no_change.changed
    assert no_change.value.state_revision == "2"

    with pytest.raises(LifecycleError) as stale:
        repository.change_workspace_state(
            _service_context(
                service_id, "workspace.restore", workspace_id=workspace_id
            ),
            workspace_id,
            WorkspaceAction.RESTORE,
            expected_revision="1",
            idempotency_key="workspace-restore-key-001",
            reason="Resume work.",
        )
    assert stale.value.code is LifecycleErrorCode.STATE_REVISION_CONFLICT
    assert stale.value.current_revision == "2"

    restored = repository.change_workspace_state(
        _service_context(service_id, "workspace.restore", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.RESTORE,
        expected_revision="2",
        idempotency_key="workspace-restore-key-002",
        reason="Resume work.",
    )
    assert restored.value.state is LifecycleState.ACTIVE
    assert restored.value.state_revision == "3"
    disabled_again = repository.change_workspace_state(
        _service_context(service_id, "workspace.disable", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.DISABLE,
        expected_revision="3",
        idempotency_key="workspace-disable-key-003",
        reason="Pause work again.",
    )
    assert disabled_again.value.state_revision == "4"

    retired = repository.change_workspace_state(
        _service_context(service_id, "workspace.retire", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.RETIRE,
        expected_revision="4",
        idempotency_key="workspace-retire-key-001",
        reason="Close the workspace.",
    )
    assert retired.value.state is LifecycleState.RETIRED
    with pytest.raises(LifecycleError) as terminal:
        repository.change_workspace_state(
            _service_context(
                service_id, "workspace.restore", workspace_id=workspace_id
            ),
            workspace_id,
            WorkspaceAction.RESTORE,
            expected_revision="5",
            idempotency_key="workspace-restore-key-003",
            reason="Invalid restore.",
        )
    assert terminal.value.code is LifecycleErrorCode.WORKSPACE_RETIRED

    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.audit_events WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone() == (6,)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM router.workspaces WHERE id = %s", (workspace_id,)
            )


def test_hidden_cross_service_read_and_descendant_admission(
    repository: PostgresLifecycleRepository,
) -> None:
    """Hide another service workspace and stop work below a disabled parent."""
    parent = _create_service(repository, name="Parent")
    child = _create_service(
        repository,
        key="service-create-key-00002",
        name="Child",
        parent=parent,
    )
    workspace_id = _create_workspace(repository, child)
    other = _create_service(
        repository,
        key="service-create-key-00003",
        name="Other",
    )
    with pytest.raises(LifecycleError) as hidden:
        repository.get_workspace(
            _service_context(other, "workspace.read", workspace_id=workspace_id),
            workspace_id,
        )
    assert hidden.value.code is LifecycleErrorCode.WORKSPACE_NOT_FOUND
    assert repository.admission_is_allowed(child, workspace_id)

    repository.change_service_state(
        _global_context(),
        parent,
        ServiceAction.DISABLE,
        expected_revision="1",
        idempotency_key="service-disable-key-0001",
        reason="Stop descendant work.",
    )
    assert not repository.admission_is_allowed(child, workspace_id)
    retained = repository.get_workspace(
        _service_context(child, "workspace.read", workspace_id=workspace_id),
        workspace_id,
    )
    assert retained.state is LifecycleState.ACTIVE


def test_service_parent_api_rejects_incompatible_budget_chain(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Return a safe lifecycle error when the new parent budget is too small."""
    parent = _create_service(repository, name="Parent")
    child = _create_service(
        repository,
        key="service-create-key-00002",
        name="Child",
    )
    budgets = PostgresBudgetRepository(database_url)
    global_context = replace(
        _global_context("budget.write"), scope=Scope(), request_id="global-budget"
    )
    budgets.put_limit(
        global_context,
        BudgetTarget(BudgetScopeKind.GLOBAL),
        hard_limit=Decimal(100),
        currency="USD",
        warning_threshold=None,
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="lifecycle-global-budget",
        now=NOW,
    )
    for service_id, amount, key in (
        (parent, Decimal(50), "lifecycle-parent-budget"),
        (child, Decimal(80), "lifecycle-child-budget"),
    ):
        budgets.put_limit(
            replace(global_context, scope=Scope(service_id)),
            BudgetTarget(BudgetScopeKind.SERVICE, service_id),
            hard_limit=amount,
            currency="USD",
            warning_threshold=None,
            reset_period=ResetPeriod.NONE,
            expected_revision="0",
            idempotency_key=key,
            now=NOW,
        )
    with pytest.raises(LifecycleError) as error:
        repository.change_service_parent(
            _global_context("service_parent.manage"),
            child,
            expected_revision="1",
            new_parent_service_id=parent,
            reason="Use the parent.",
        )
    assert error.value.code is LifecycleErrorCode.INVALID_REQUEST


def test_service_parent_waits_for_budget_lock_before_service_row(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Keep service-row locks after the shared budget hierarchy lock."""
    parent = _create_service(repository, name="Parent")
    child = _create_service(
        repository,
        key="service-create-key-00002",
        name="Child",
    )
    budgets = PostgresBudgetRepository(database_url)
    budget_context = replace(
        _global_context("budget.write"), scope=Scope(), request_id="global-budget"
    )
    idempotency_key = "lifecycle-lock-order-budget"
    operation_lock = f"budget-limit:{budget_context.actor_id}:{idempotency_key}"
    with psycopg.connect(database_url) as blocker:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (operation_lock,),
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            budget_write = executor.submit(
                budgets.put_limit,
                budget_context,
                BudgetTarget(BudgetScopeKind.GLOBAL),
                hard_limit=Decimal(100),
                currency="USD",
                warning_threshold=None,
                reset_period=ResetPeriod.NONE,
                expected_revision="0",
                idempotency_key=idempotency_key,
                now=NOW,
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                budget_write.result(timeout=0.1)
            parent_write = executor.submit(
                repository.change_service_parent,
                _global_context("service_parent.manage"),
                child,
                expected_revision="1",
                new_parent_service_id=parent,
                reason="Use the parent.",
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                parent_write.result(timeout=0.1)
            with psycopg.connect(database_url) as observer:
                observer.execute(
                    "SELECT id FROM router.services WHERE id = %s FOR UPDATE NOWAIT",
                    (child,),
                )
            blocker.commit()
            assert budget_write.result(timeout=5).hard_limit.amount == Decimal(100)
            assert parent_write.result(timeout=5).value.parent_service_id == parent
        finally:
            blocker.rollback()
            executor.shutdown(wait=True, cancel_futures=True)


def test_parent_cycle_and_restore_guard_fail_closed(
    repository: PostgresLifecycleRepository,
    database_url: str,
) -> None:
    """Reject a cycle and deny restore when current eligibility fails."""
    parent = _create_service(repository, name="Parent")
    child = _create_service(
        repository,
        key="service-create-key-00002",
        name="Child",
        parent=parent,
    )
    with pytest.raises(LifecycleError) as cycle:
        repository.change_service_parent(
            _global_context("service_parent.manage"),
            parent,
            expected_revision="1",
            new_parent_service_id=child,
            reason="Invalid cycle.",
        )
    assert cycle.value.code is LifecycleErrorCode.INVALID_REQUEST

    guarded = PostgresLifecycleRepository(
        database_url,
        workspace_restore_is_eligible=lambda _service, _workspace: False,
    )
    workspace_id = _create_workspace(repository, child)
    repository.change_workspace_state(
        _service_context(child, "workspace.disable", workspace_id=workspace_id),
        workspace_id,
        WorkspaceAction.DISABLE,
        expected_revision="1",
        idempotency_key=DISABLE_KEY,
        reason="Pause work.",
    )
    with pytest.raises(LifecycleError) as unavailable:
        guarded.change_workspace_state(
            _service_context(child, "workspace.restore", workspace_id=workspace_id),
            workspace_id,
            WorkspaceAction.RESTORE,
            expected_revision="2",
            idempotency_key="workspace-restore-key-001",
            reason="Resume work.",
        )
    assert unavailable.value.code is LifecycleErrorCode.WORKSPACE_UNAVAILABLE

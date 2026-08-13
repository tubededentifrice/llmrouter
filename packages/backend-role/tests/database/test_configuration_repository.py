"""PostgreSQL configuration publication and isolation tests."""
# ruff: noqa: D103

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

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
from llmrouter_backend.configuration import (
    Assignment,
    AssignmentCandidate,
    CatalogEntry,
    CatalogKind,
    ConfigurationError,
    ConfigurationErrorCode,
    ConfigurationScope,
    ConfigurationState,
    PostgresConfigurationRepository,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    RegisteredSchema,
    ScopeConfiguration,
    SettingsSchemaRegistry,
)
from llmrouter_backend.database import migrate

from .helpers import OTHER_SERVICE_ID, SERVICE_ID, seed_scope

NOW = datetime(2026, 8, 13, 16, tzinfo=UTC)
MODEL_ID = "0198a080-0000-7000-8000-000000000090"
INSTANCE_ID = "0198a080-0000-7000-8000-000000000091"
ROUTE_ID = "0198a080-0000-7000-8000-000000000092"
LATER_ROUTE_ID = "0198a080-0000-7000-8000-000000000093"
CREDENTIAL_ID = "0198a080-0000-7000-8000-000000000094"


def _registry() -> SettingsSchemaRegistry:
    return SettingsSchemaRegistry(
        (
            RegisteredSchema("provider.settings", 1, {"region": str}),
            RegisteredSchema("route.settings", 1, {"tier": str}),
            RegisteredSchema("model.metadata", 1, {"family": str}),
        )
    )


def _context(
    operation: str,
    *,
    service_id: str | None = None,
    workspace_id: str | None = None,
    mutation: bool = True,
) -> RequestContext:
    return RequestContext(
        request_id=f"configuration-{operation}",
        actor_kind=(
            PrincipalKind.ADMINISTRATOR if service_id is None else PrincipalKind.SERVICE
        ),
        actor_id="issuer:administrator" if service_id is None else service_id,
        authority_class=(
            AuthorityClass.GLOBAL_ADMINISTRATOR
            if service_id is None
            else AuthorityClass.SERVICE
        ),
        authority_path=(
            AuthorityPath.GLOBAL_ADMINISTRATION
            if service_id is None
            else AuthorityPath.MACHINE
        ),
        machine_audience=None if service_id is None else Audience.CONFIGURATION,
        operation=operation,
        scope=Scope(service_id, workspace_id),
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=mutation,
    )


def _distribution_context() -> RequestContext:
    return RequestContext(
        request_id="configuration-distribution-observe",
        actor_kind=PrincipalKind.SYSTEM,
        actor_id="configuration-distributor",
        authority_class=AuthorityClass.SYSTEM,
        authority_path=AuthorityPath.MACHINE,
        machine_audience=None,
        operation="configuration.distribution.observe",
        scope=Scope(),
        authorized_at=NOW,
        recent_authentication_at=None,
        mutation=True,
    )


def _document(name: str, **values: object) -> RegisteredDocument:
    return RegisteredDocument(name, 1, values)


def _content(*, routes: tuple[str, ...] = (ROUTE_ID,)) -> ScopeConfiguration:
    route_values = tuple(
        ProviderModelRoute(
            route_id,
            INSTANCE_ID,
            MODEL_ID,
            f"wire-{index}",
            frozenset({"text"}),
            _document("route.settings", tier="normal"),
        )
        for index, route_id in enumerate(routes)
    )
    return ScopeConfiguration(
        catalog=(
            CatalogEntry(
                CatalogKind.PROVIDER,
                "provider.example",
                "Provider",
                frozenset({"text"}),
                settings=_document("provider.settings", region="eu"),
            ),
            CatalogEntry(
                CatalogKind.MODEL,
                MODEL_ID,
                "Model",
                frozenset({"text"}),
                settings=_document("model.metadata", family="test"),
            ),
        ),
        provider_instances=(
            ProviderInstance(
                INSTANCE_ID,
                "provider.example",
                "Provider",
                "https://provider.example",
                CREDENTIAL_ID,
                _document("provider.settings", region="eu"),
            ),
        ),
        provider_model_routes=route_values,
        assignments=(
            Assignment("chat", (AssignmentCandidate(routes[0]),), frozenset({"text"})),
        ),
    )


@pytest.fixture
def repository(database_url: str) -> PostgresConfigurationRepository:
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        seed_scope(connection)
        connection.execute(
            """
            INSERT INTO router.encrypted_credentials (
                id, owner_kind, credential_kind, ciphertext, encrypted_data_key,
                wrapping_key_id, safe_fingerprint, current_revision,
                last_changed_at
            ) VALUES (%s, 'global', 'provider.example', %s, %s, 'wrap-test',
                      'safe', %s, %s)
            """,
            (CREDENTIAL_ID, bytes(32), bytes(32), CREDENTIAL_ID, NOW),
        )
    return PostgresConfigurationRepository(database_url, schema_registry=_registry())


def _publish_global(repository: PostgresConfigurationRepository) -> str:
    try:
        return repository.publish(
            _context("catalog.manage"),
            ConfigurationScope(),
            _content(),
            expected_active_revision=None,
            reason="Publish the test catalog.",
            now=NOW,
        ).active_revision
    except ConfigurationError as error:
        pytest.fail(f"Unexpected configuration issues: {error.issues!r}")


def test_publish_is_atomic_audited_and_resolves_isolated_scope(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    revision = _publish_global(repository)
    effective = repository.effective(
        _context("configuration.read", service_id=SERVICE_ID, mutation=False),
        ConfigurationScope(service_id=SERVICE_ID),
    )
    assert effective.assignments[0].active_revision == revision
    assert effective.assignments[0].inherited
    assert effective.distribution_state.value == "distributing"
    with pytest.raises(ConfigurationError) as denied:
        repository.effective(
            _context("configuration.read", service_id=OTHER_SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
    assert denied.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT revision_number, restored_from_revision_id,
                   (SELECT count(*) FROM router.configuration_audit_bindings)
            FROM router.configuration_revisions WHERE id = %s
            """,
            (revision,),
        ).fetchone()
    assert row == (1, None, 1)


def test_global_effective_read_fails_without_a_raw_resolver_crash(
    repository: PostgresConfigurationRepository,
) -> None:
    _publish_global(repository)
    with pytest.raises(ConfigurationError) as denied:
        repository.effective(
            _context("catalog.manage", mutation=False), ConfigurationScope()
        )
    assert denied.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE


def test_scoped_authority_rejects_forged_actor_path_audience_and_scope(
    repository: PostgresConfigurationRepository,
) -> None:
    _publish_global(repository)
    valid = _context("configuration.read", service_id=SERVICE_ID, mutation=False)
    forged = (
        replace(valid, actor_kind=PrincipalKind.ADMINISTRATOR),
        replace(valid, authority_path=AuthorityPath.EMBED, machine_audience=None),
        replace(valid, machine_audience=Audience.DATA_PLANE),
        replace(valid, actor_id=OTHER_SERVICE_ID),
        replace(valid, scope=Scope(SERVICE_ID, str(uuid.uuid4()))),
    )
    for context in forged:
        with pytest.raises(ConfigurationError) as denied:
            repository.effective(context, ConfigurationScope(service_id=SERVICE_ID))
        assert denied.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE
    administrator = replace(
        valid,
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="issuer:administrator",
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="provider_instance.manage",
        mutation=True,
    )
    with pytest.raises(ConfigurationError) as denied_write:
        repository.publish(
            administrator,
            ConfigurationScope(service_id=SERVICE_ID),
            ScopeConfiguration(),
            expected_active_revision=None,
            reason="Reject a cross-path scoped write.",
            now=NOW,
        )
    assert denied_write.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE


def test_service_eligibility_must_stay_in_the_owner_tree(
    repository: PostgresConfigurationRepository,
) -> None:
    _publish_global(repository)
    service_content = ScopeConfiguration(
        provider_instances=(
            replace(
                _content().provider_instances[0],
                provider_instance_id=str(uuid.uuid4()),
                eligible_service_ids=frozenset({OTHER_SERVICE_ID}),
            ),
        )
    )
    with pytest.raises(ConfigurationError) as invalid:
        repository.publish(
            _context("configuration.write", service_id=SERVICE_ID),
            ConfigurationScope(service_id=SERVICE_ID),
            service_content,
            expected_active_revision=None,
            reason="Reject an unrelated eligible service.",
            now=NOW + timedelta(seconds=1),
        )
    assert any("owning service tree" in issue.reason for issue in invalid.value.issues)


def test_concurrent_expected_revision_allows_one_save(
    repository: PostgresConfigurationRepository,
) -> None:
    revision = _publish_global(repository)

    def save(reason: str) -> str:
        changed = replace(
            _content(),
            assignments=(Assignment("chat", (AssignmentCandidate(ROUTE_ID, 31_000),)),),
        )
        return repository.publish(
            _context("catalog.manage"),
            ConfigurationScope(),
            changed,
            expected_active_revision=revision,
            reason=reason,
            now=NOW + timedelta(seconds=1),
        ).active_revision

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save, value) for value in ("Save one.", "Save two.")]
    results: list[str] = []
    conflicts = 0
    for future in futures:
        try:
            results.append(future.result())
        except ConfigurationError:
            conflicts += 1
    assert len(results) == 1
    assert conflicts == 1


def test_assignment_projection_preserves_exact_millisecond_timeout(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    first = _publish_global(repository)
    changed = replace(
        _content(),
        assignments=(Assignment("chat", (AssignmentCandidate(ROUTE_ID, 30_001),)),),
    )
    second = repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        changed,
        expected_active_revision=first,
        reason="Preserve the exact attempt timeout.",
        now=NOW + timedelta(seconds=1),
    ).active_revision
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT attempt_timeout_seconds, attempt_timeout_ms
            FROM router.assignment_candidates
            WHERE configuration_revision_id = %s
            """,
            (second,),
        ).fetchone()
    assert row == (31, 30_001)


def test_provider_display_name_and_wire_model_are_mutable_metadata(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    first = _publish_global(repository)
    original = _content()
    changed = replace(
        original,
        provider_instances=(
            replace(original.provider_instances[0], display_name="Changed provider"),
        ),
        provider_model_routes=(
            replace(original.provider_model_routes[0], wire_model="changed-wire"),
        ),
    )
    repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        changed,
        expected_active_revision=first,
        reason="Change mutable provider metadata.",
        now=NOW + timedelta(seconds=1),
    )
    with psycopg.connect(database_url) as connection:
        instance = connection.execute(
            """
            SELECT stable_name, display_name FROM router.provider_instances
            WHERE id = %s
            """,
            (INSTANCE_ID,),
        ).fetchone()
        route = connection.execute(
            """
            SELECT provider_lookup_id, wire_model FROM router.provider_model_routes
            WHERE id = %s
            """,
            (ROUTE_ID,),
        ).fetchone()
    assert instance == (INSTANCE_ID, "Changed provider")
    assert route == (ROUTE_ID, "changed-wire")


def test_distribution_observations_are_authenticated_ordered_snapshots(
    repository: PostgresConfigurationRepository,
) -> None:
    revision = _publish_global(repository)
    context = _distribution_context()
    assert (
        repository.mark_distribution(
            context,
            revision,
            current_nodes=0,
            total_nodes=0,
            degraded=False,
            observed_at=NOW,
        ).value
        == "distributing"
    )
    with pytest.raises(ValueError, match="conflicts"):
        repository.mark_distribution(
            context,
            revision,
            current_nodes=0,
            total_nodes=1,
            degraded=False,
            observed_at=NOW,
        )
    with pytest.raises(ValueError, match="stale"):
        repository.mark_distribution(
            context,
            revision,
            current_nodes=0,
            total_nodes=1,
            degraded=False,
            observed_at=NOW - timedelta(microseconds=1),
        )
    assert (
        repository.mark_distribution(
            context,
            revision,
            current_nodes=2,
            total_nodes=2,
            degraded=False,
            observed_at=NOW + timedelta(seconds=1),
        ).value
        == "current"
    )
    assert (
        repository.mark_distribution(
            context,
            revision,
            current_nodes=1,
            total_nodes=2,
            degraded=True,
            observed_at=NOW + timedelta(seconds=2),
        ).value
        == "degraded"
    )
    assert (
        repository.mark_distribution(
            context,
            revision,
            current_nodes=2,
            total_nodes=2,
            degraded=False,
            observed_at=NOW + timedelta(seconds=3),
        ).value
        == "current"
    )
    with pytest.raises(ConfigurationError) as denied:
        repository.mark_distribution(
            replace(context, actor_kind=PrincipalKind.SERVICE),
            revision,
            current_nodes=2,
            total_nodes=2,
            degraded=False,
            observed_at=NOW + timedelta(seconds=4),
        )
    assert denied.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE


def test_rollback_restores_earlier_content_and_retires_later_identity(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    first = _publish_global(repository)
    second = repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        _content(routes=(ROUTE_ID, LATER_ROUTE_ID)),
        expected_active_revision=first,
        reason="Add a second route.",
        now=NOW + timedelta(seconds=1),
    ).active_revision
    with_later = _content(routes=(ROUTE_ID, LATER_ROUTE_ID))
    retired_later = replace(
        with_later,
        provider_model_routes=(
            with_later.provider_model_routes[0],
            replace(
                with_later.provider_model_routes[1],
                state=ConfigurationState.RETIRED,
            ),
        ),
    )
    third = repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        retired_later,
        expected_active_revision=second,
        reason="Retire the second route.",
        now=NOW + timedelta(seconds=2),
    ).active_revision
    rollback = repository.rollback(
        _context("catalog.manage"),
        ConfigurationScope(),
        first,
        expected_active_revision=third,
        reason="Restore the earlier configuration.",
        now=NOW + timedelta(seconds=3),
    )
    effective = repository.effective(
        _context("configuration.read", service_id=SERVICE_ID, mutation=False),
        ConfigurationScope(service_id=SERVICE_ID),
    )
    assert [item.stable_id for item in effective.provider_model_routes] == [ROUTE_ID]
    assert effective.active_revision == rollback.active_revision
    with psycopg.connect(database_url) as connection:
        later_state = connection.execute(
            "SELECT state FROM router.provider_model_routes WHERE id = %s",
            (LATER_ROUTE_ID,),
        ).fetchone()
        restored = connection.execute(
            """
            SELECT restored_from_revision_id FROM router.configuration_revisions
            WHERE id = %s
            """,
            (rollback.active_revision,),
        ).fetchone()
    assert later_state == ("retired",)
    assert restored == (uuid.UUID(first),)

"""PostgreSQL configuration publication and isolation tests."""
# ruff: noqa: D103

from __future__ import annotations

import concurrent.futures
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from llmrouter_backend.accounting import (
    PostgresAccountingRepository,
    SourceSnapshot,
    SynchronizationStatus,
)
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
    PriceAuthority,
    PriceAuthorityMode,
    PriceComponent,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    RegisteredSchema,
    ScopeConfiguration,
    SettingsSchemaRegistry,
    SynchronizationState,
    UsageUnit,
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
            PriceAuthority(PriceAuthorityMode.SOURCE, "catalog-test", f"wire-{index}"),
            (
                PriceComponent(
                    UsageUnit.INPUT_TOKEN,
                    Decimal("0.001"),
                    "USD",
                    "0.001",
                    Decimal(1000),
                ),
            ),
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


def _source_snapshot(
    fetched_at: datetime,
    rows: dict[str, tuple[PriceComponent, ...]],
) -> SourceSnapshot:
    return SourceSnapshot("catalog-test", fetched_at, SourceSnapshot.digest(rows), rows)


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
    permitted = repository.publish(
        administrator,
        ConfigurationScope(service_id=SERVICE_ID),
        ScopeConfiguration(),
        expected_active_revision=None,
        reason="Permit one exact-scope administrator write.",
        now=NOW,
    )
    assert permitted.resource_id == SERVICE_ID
    for denied_context in (
        replace(administrator, operation="configuration.read"),
        replace(administrator, scope=Scope(OTHER_SERVICE_ID)),
    ):
        with pytest.raises(ConfigurationError) as denied_write:
            repository.publish(
                denied_context,
                ConfigurationScope(service_id=SERVICE_ID),
                ScopeConfiguration(),
                expected_active_revision=permitted.active_revision,
                reason="Reject one invalid administrator write.",
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


def test_route_prices_pin_unpin_schedule_and_rollback_are_revisioned(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    """Round-trip exact route prices and restore one manual price policy."""
    first = _publish_global(repository)
    effective = repository.effective(
        _context("configuration.read", service_id=SERVICE_ID, mutation=False),
        ConfigurationScope(service_id=SERVICE_ID),
    )
    first_route = effective.provider_model_routes[0].value
    assert isinstance(first_route, ProviderModelRoute)
    assert first_route.price_authority == PriceAuthority(
        PriceAuthorityMode.SOURCE, "catalog-test", "wire-0"
    )
    assert first_route.price_version is not None
    assert first_route.synchronization_schedule == "0 0 * * 0"
    assert first_route.stale_after_seconds == 14 * 24 * 60 * 60
    accounting = PostgresAccountingRepository(database_url)
    missing_sync = accounting.synchronize(
        _context("provider_route.manage"),
        service_id=None,
        snapshot=_source_snapshot(NOW + timedelta(milliseconds=500), {}),
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(milliseconds=500),
    )
    assert missing_sync.rows[0].status is SynchronizationStatus.MISSING
    stale_route = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(stale_route, ProviderModelRoute)
    assert stale_route.synchronization_state is SynchronizationState.STALE

    manual_price = PriceComponent(
        UsageUnit.INPUT_TOKEN, Decimal("0.002"), "USD", "0.002", Decimal(1000)
    )
    manual_content = replace(
        _content(),
        provider_model_routes=(
            replace(
                _content().provider_model_routes[0],
                price_authority=PriceAuthority(PriceAuthorityMode.MANUAL),
                prices=(manual_price,),
                synchronization_schedule="0 6 * * 1",
                stale_after_seconds=3 * 24 * 60 * 60,
            ),
        ),
    )
    manual_revision = repository.publish(
        _context("provider_route.manage"),
        ConfigurationScope(),
        manual_content,
        expected_active_revision=first,
        reason="Pin the route price.",
        now=NOW + timedelta(seconds=1),
    ).active_revision
    manual_route = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(manual_route, ProviderModelRoute)
    manual_version = manual_route.price_version
    source_price = PriceComponent(
        UsageUnit.INPUT_TOKEN, Decimal("0.003"), "USD", "0.003", Decimal(1000)
    )
    snapshot = _source_snapshot(NOW + timedelta(seconds=2), {"wire-0": (source_price,)})
    pinned_sync = accounting.synchronize(
        _context("provider_route.manage"),
        service_id=None,
        snapshot=snapshot,
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(seconds=2),
    )
    assert pinned_sync.rows[0].status is SynchronizationStatus.SKIPPED
    assert pinned_sync.resulting_configuration_revision is None
    unpinned_content = replace(
        manual_content,
        provider_model_routes=(
            replace(
                manual_content.provider_model_routes[0],
                price_authority=PriceAuthority(
                    PriceAuthorityMode.SOURCE, "catalog-test", "wire-0"
                ),
            ),
        ),
    )
    repository.publish(
        _context("provider_route.manage"),
        ConfigurationScope(),
        unpinned_content,
        expected_active_revision=manual_revision,
        reason="Remove the manual route price pin.",
        now=NOW + timedelta(seconds=2),
    )
    synchronized = accounting.synchronize(
        _context("provider_route.manage"),
        service_id=None,
        snapshot=replace(snapshot, fetched_at=NOW + timedelta(seconds=3)),
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(seconds=3),
    )
    assert synchronized.rows[0].status is SynchronizationStatus.UPDATED
    assert synchronized.resulting_configuration_revision is not None
    synchronized_route = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(synchronized_route, ProviderModelRoute)
    assert synchronized_route.prices == (
        PriceComponent(
            UsageUnit.INPUT_TOKEN,
            Decimal("0.003"),
            "USD",
            "0.003",
            Decimal(1000),
        ),
    )
    rollback = repository.rollback(
        _context("provider_route.manage"),
        ConfigurationScope(),
        manual_revision,
        expected_active_revision=synchronized.resulting_configuration_revision,
        reason="Restore the manual route price pin.",
        now=NOW + timedelta(seconds=3),
    )
    restored = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(restored, ProviderModelRoute)
    assert restored.price_authority.mode is PriceAuthorityMode.MANUAL
    assert restored.prices == (manual_price,)
    assert restored.synchronization_schedule == "0 6 * * 1"
    assert restored.stale_after_seconds == 3 * 24 * 60 * 60
    assert restored.price_version == manual_version
    with psycopg.connect(database_url) as connection:
        projection = connection.execute(
            """
            SELECT source.authority_kind, source.source_name,
                   source.lookup_identifier, source.synchronization_schedule,
                   extract(epoch FROM source.stale_after)::bigint,
                   state.synchronization_state,
                   (SELECT count(*) FROM router.route_price_versions
                    WHERE provider_model_route_id = %s),
                   (SELECT count(*) FROM router.configuration_price_bindings
                    WHERE provider_model_route_id = %s)
            FROM router.route_price_sources AS source
            JOIN router.route_price_synchronization_states AS state
              ON state.provider_model_route_id = source.provider_model_route_id
            WHERE source.provider_model_route_id = %s
            """,
            (ROUTE_ID, ROUTE_ID, ROUTE_ID),
        ).fetchone()
    assert projection == (
        "manual",
        None,
        None,
        "0 6 * * 1",
        3 * 24 * 60 * 60,
        "manual",
        3,
        5,
    )
    assert rollback.active_revision != manual_revision


def test_source_price_edit_is_rejected_and_metadata_preserves_stale_state(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    """Keep a source price under synchronization and preserve newer state."""
    first = _publish_global(repository)
    direct_price = replace(
        _content(),
        provider_model_routes=(
            replace(
                _content().provider_model_routes[0],
                prices=(
                    PriceComponent(
                        UsageUnit.INPUT_TOKEN,
                        Decimal("0.002"),
                        "USD",
                        "0.002",
                        Decimal(1000),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ConfigurationError) as rejected:
        repository.publish(
            _context("provider_route.manage"),
            ConfigurationScope(),
            direct_price,
            expected_active_revision=first,
            reason="Reject a direct source price edit.",
            now=NOW + timedelta(seconds=1),
        )
    assert any(
        "only by synchronization" in item.reason for item in rejected.value.issues
    )

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE router.route_price_synchronization_states
               SET synchronization_state = 'current',
                   observed_at = %s
               WHERE provider_model_route_id = %s""",
            (NOW - timedelta(days=15), ROUTE_ID),
        )
    effective = repository.effective(
        _context("configuration.read", service_id=SERVICE_ID, mutation=False),
        ConfigurationScope(service_id=SERVICE_ID),
    )
    aged_route = effective.provider_model_routes[0].value
    assert isinstance(aged_route, ProviderModelRoute)
    assert aged_route.synchronization_state is SynchronizationState.STALE

    changed = replace(
        _content(),
        provider_model_routes=(
            replace(_content().provider_model_routes[0], wire_model="metadata-only"),
        ),
    )
    repository.publish(
        _context("provider_route.manage"),
        ConfigurationScope(),
        changed,
        expected_active_revision=first,
        reason="Change route metadata without replacing price state.",
        now=NOW + timedelta(seconds=2),
    )
    preserved = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(preserved, ProviderModelRoute)
    assert preserved.synchronization_state is SynchronizationState.STALE


def test_source_route_can_start_without_a_price_version(
    database_url: str,
    repository: PostgresConfigurationRepository,
) -> None:
    """Keep a migrated source route visible until its first price sync."""
    content = replace(
        _content(),
        provider_model_routes=(
            replace(
                _content().provider_model_routes[0],
                prices=(),
                synchronization_state=SynchronizationState.MISSING,
            ),
        ),
    )
    repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        content,
        expected_active_revision=None,
        reason="Publish one source route that needs its first price sync.",
        now=NOW,
    )
    route = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(route, ProviderModelRoute)
    assert route.prices == ()
    assert route.price_version is None
    assert route.synchronization_state is SynchronizationState.MISSING

    result = PostgresAccountingRepository(database_url).synchronize(
        _context("provider_route.manage"),
        service_id=None,
        snapshot=_source_snapshot(
            NOW + timedelta(seconds=1),
            {
                "wire-0": (
                    PriceComponent(
                        UsageUnit.INPUT_TOKEN,
                        Decimal("0.001"),
                        "USD",
                        "0.001",
                        Decimal(1000),
                    ),
                )
            },
        ),
        route_ids=(ROUTE_ID,),
        dry_run=False,
        now=NOW + timedelta(seconds=1),
    )
    assert result.rows[0].status is SynchronizationStatus.UPDATED
    updated = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(updated, ProviderModelRoute)
    assert updated.price_version is not None
    assert updated.synchronization_state is SynchronizationState.CURRENT


def test_legacy_revision_without_price_fields_remains_visible(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    """Read one immutable pre-pricing revision without changing its content."""
    first = _publish_global(repository)
    legacy_revision = str(uuid.uuid4())
    with psycopg.connect(database_url) as connection:
        stored = connection.execute(
            "SELECT content FROM router.configuration_revisions WHERE id = %s",
            (first,),
        ).fetchone()
        assert stored is not None
        legacy_content = stored[0]
        for route in legacy_content["provider_model_routes"]:
            for field in (
                "price_authority",
                "prices",
                "synchronization_schedule",
                "stale_after_seconds",
                "price_version",
                "synchronization_state",
            ):
                route.pop(field)
        connection.execute(
            """INSERT INTO router.configuration_revisions (
                   id, scope_kind, revision_number, content, content_sha256,
                   created_at, created_by_kind, created_by_id
               ) VALUES (%s, 'global', 2, %s::jsonb,
                         decode(repeat('ab', 32), 'hex'), %s,
                         'system', 'migration-test')""",
            (legacy_revision, json.dumps(legacy_content), NOW + timedelta(seconds=1)),
        )
        connection.execute(
            """UPDATE router.active_configurations
               SET revision_id = %s, revision_number = 2, activated_at = %s
               WHERE scope_kind = 'global' AND service_id IS NULL
                 AND workspace_id IS NULL""",
            (legacy_revision, NOW + timedelta(seconds=1)),
        )
        connection.execute(
            "DELETE FROM router.route_price_sources WHERE provider_model_route_id = %s",
            (ROUTE_ID,),
        )
    route = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert isinstance(route, ProviderModelRoute)
    assert route.price_authority == PriceAuthority(
        PriceAuthorityMode.SOURCE, "legacy-unconfigured", "wire-0"
    )
    assert route.prices == ()
    assert route.price_version is None
    assert route.synchronization_state is SynchronizationState.MISSING

    manual_content = replace(
        _content(),
        provider_model_routes=(
            replace(
                _content().provider_model_routes[0],
                price_authority=PriceAuthority(PriceAuthorityMode.MANUAL),
            ),
        ),
    )
    modern = repository.publish(
        _context("provider_route.manage"),
        ConfigurationScope(),
        manual_content,
        expected_active_revision=legacy_revision,
        reason="Publish one price-aware revision.",
        now=NOW + timedelta(seconds=2),
    )
    rollback = repository.rollback(
        _context("provider_route.manage"),
        ConfigurationScope(),
        legacy_revision,
        expected_active_revision=modern.active_revision,
        reason="Restore the legacy route revision.",
        now=NOW + timedelta(seconds=3),
    )
    restored = (
        repository.effective(
            _context("configuration.read", service_id=SERVICE_ID, mutation=False),
            ConfigurationScope(service_id=SERVICE_ID),
        )
        .provider_model_routes[0]
        .value
    )
    assert rollback.active_revision != legacy_revision
    assert isinstance(restored, ProviderModelRoute)
    assert restored.price_authority.mode is PriceAuthorityMode.MANUAL


def test_service_owned_route_price_rejects_cross_scope_write(
    database_url: str, repository: PostgresConfigurationRepository
) -> None:
    """Keep a service-owned price authority in its exact service scope."""
    global_content = replace(_content(), provider_model_routes=(), assignments=())
    repository.publish(
        _context("catalog.manage"),
        ConfigurationScope(),
        global_content,
        expected_active_revision=None,
        reason="Publish the shared provider without a global route.",
        now=NOW,
    )
    service_content = ScopeConfiguration(
        provider_model_routes=_content().provider_model_routes,
        assignments=_content().assignments,
    )
    service_revision = repository.publish(
        _context("configuration.write", service_id=SERVICE_ID),
        ConfigurationScope(service_id=SERVICE_ID),
        service_content,
        expected_active_revision=None,
        reason="Publish a service-owned priced route.",
        now=NOW + timedelta(seconds=1),
    ).active_revision
    with pytest.raises(ConfigurationError) as denied:
        repository.publish(
            _context("configuration.write", service_id=OTHER_SERVICE_ID),
            ConfigurationScope(service_id=SERVICE_ID),
            service_content,
            expected_active_revision=service_revision,
            reason="Reject a cross-service route price edit.",
            now=NOW + timedelta(seconds=2),
        )
    assert denied.value.code is ConfigurationErrorCode.INSUFFICIENT_SCOPE
    with psycopg.connect(database_url) as connection:
        owner = connection.execute(
            """SELECT owner_kind, owner_service_id::text
               FROM router.provider_model_routes WHERE id = %s""",
            (ROUTE_ID,),
        ).fetchone()
    assert owner == ("service", SERVICE_ID)


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

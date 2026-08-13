"""Deterministic configuration resolver and schema tests."""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from llmrouter_backend.configuration import (
    Assignment,
    AssignmentCandidate,
    CatalogEntry,
    CatalogKind,
    ConfigurationScope,
    DistributionState,
    InheritedDisable,
    PriceAuthority,
    PriceAuthorityMode,
    PriceComponent,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    RegisteredSchema,
    ResourceKind,
    RevisionLayer,
    ScopeConfiguration,
    SettingsSchemaRegistry,
    SynchronizationState,
    UsageUnit,
    resolve_configuration,
    validate_endpoint,
    validate_layers,
)

SERVICE_A = "0198a080-0000-7000-8000-000000000001"
SERVICE_B = "0198a080-0000-7000-8000-000000000002"
WORKSPACE = "0198a080-0000-7000-8000-000000000003"
SERVICE_C = "0198a080-0000-7000-8000-000000000009"
MODEL = "0198a080-0000-7000-8000-000000000004"
CREDENTIAL = "0198a080-0000-7000-8000-000000000005"
INSTANCE = "0198a080-0000-7000-8000-000000000006"
ROUTE_A = "0198a080-0000-7000-8000-000000000007"
ROUTE_B = "0198a080-0000-7000-8000-000000000008"


def _registry() -> SettingsSchemaRegistry:
    return SettingsSchemaRegistry(
        (
            RegisteredSchema(
                "provider.settings",
                1,
                {"region": str},
                frozenset({"region"}),
            ),
            RegisteredSchema("route.settings", 1, {"tier": str}),
            RegisteredSchema("model.metadata", 1, {"family": str}),
        )
    )


def _document(name: str, document: dict[str, object]) -> RegisteredDocument:
    return RegisteredDocument(name, 1, document)


def _global_content() -> ScopeConfiguration:
    return ScopeConfiguration(
        catalog=(
            CatalogEntry(
                CatalogKind.PROVIDER,
                "provider.example",
                "Provider",
                frozenset({"text"}),
                settings=_document("provider.settings", {"region": "eu"}),
            ),
            CatalogEntry(
                CatalogKind.MODEL,
                MODEL,
                "Model",
                frozenset({"text"}),
                settings=_document("model.metadata", {"family": "test"}),
            ),
        ),
        provider_instances=(
            ProviderInstance(
                INSTANCE,
                "provider.example",
                "Instance",
                "https://provider.example",
                CREDENTIAL,
                _document("provider.settings", {"region": "eu"}),
            ),
        ),
        provider_model_routes=(
            ProviderModelRoute(
                ROUTE_A,
                INSTANCE,
                MODEL,
                "model-a",
                frozenset({"text"}),
                _document("route.settings", {"tier": "normal"}),
                PriceAuthority(PriceAuthorityMode.MANUAL),
                (
                    PriceComponent(
                        UsageUnit.INPUT_TOKEN, Decimal("0.001"), "USD", "0.001"
                    ),
                ),
            ),
            ProviderModelRoute(
                ROUTE_B,
                INSTANCE,
                MODEL,
                "model-b",
                frozenset({"text"}),
                _document("route.settings", {"tier": "normal"}),
                PriceAuthority(PriceAuthorityMode.MANUAL),
                (
                    PriceComponent(
                        UsageUnit.INPUT_TOKEN, Decimal("0.002"), "USD", "0.002"
                    ),
                ),
            ),
        ),
        assignments=(
            Assignment("chat", (AssignmentCandidate(ROUTE_A),), frozenset({"text"})),
        ),
    )


def test_nearest_layer_replaces_complete_assignment_chain() -> None:
    layers = (
        RevisionLayer(ConfigurationScope(), "global-revision", _global_content()),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A),
            "service-revision",
            ScopeConfiguration(
                assignments=(Assignment("chat", (AssignmentCandidate(ROUTE_B),)),)
            ),
        ),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A, workspace_id=WORKSPACE),
            "workspace-revision",
            ScopeConfiguration(),
        ),
    )
    effective = resolve_configuration(
        layers,
        registry=_registry(),
        distribution_state=DistributionState.DISTRIBUTING,
    )
    assignment = effective.assignments[0]
    assert assignment.source_layer == SERVICE_A
    assert assignment.inherited
    assert isinstance(assignment.value, Assignment)
    assert assignment.value.candidates == (AssignmentCandidate(ROUTE_B),)


def test_child_disable_is_effective_for_descendants() -> None:
    layers = (
        RevisionLayer(ConfigurationScope(), "global-revision", _global_content()),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A),
            "service-revision",
            ScopeConfiguration(
                inherited_disables=(
                    InheritedDisable(ResourceKind.PROVIDER_MODEL_ROUTE, ROUTE_A),
                )
            ),
        ),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A, workspace_id=WORKSPACE),
            "workspace-revision",
            ScopeConfiguration(),
        ),
    )
    issues = validate_layers(layers, registry=_registry())
    assert any("not eligible for new work" in item.reason for item in issues)


def test_service_eligibility_does_not_use_ancestors_above_the_owner() -> None:
    service_content = ScopeConfiguration(
        provider_instances=(
            replace(
                _global_content().provider_instances[0],
                eligible_service_ids=frozenset({SERVICE_B}),
            ),
        ),
        provider_model_routes=(
            replace(
                _global_content().provider_model_routes[0],
                eligible_service_ids=frozenset({SERVICE_B}),
            ),
        ),
    )
    layers = (
        RevisionLayer(ConfigurationScope(), "global", _global_content()),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_B),
            "parent",
            ScopeConfiguration(),
        ),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A), "owner", service_content
        ),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_C),
            "descendant",
            ScopeConfiguration(),
        ),
    )
    effective = resolve_configuration(
        layers,
        registry=_registry(),
        distribution_state=DistributionState.DISTRIBUTING,
    )
    assert effective.provider_instances[0].source_layer == "global"
    assert effective.provider_model_routes[0].source_layer == "global"


def test_hidden_provider_resources_do_not_invalidate_an_unrelated_service() -> None:
    restricted = replace(
        _global_content(),
        provider_instances=(
            replace(
                _global_content().provider_instances[0],
                eligible_service_ids=frozenset({SERVICE_A}),
            ),
        ),
        provider_model_routes=(
            replace(
                _global_content().provider_model_routes[0],
                eligible_service_ids=frozenset({SERVICE_A}),
            ),
        ),
        assignments=(),
    )
    service_a = (
        RevisionLayer(ConfigurationScope(), "global", restricted),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A), "a", ScopeConfiguration()
        ),
    )
    service_b = (
        RevisionLayer(ConfigurationScope(), "global", restricted),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_B), "b", ScopeConfiguration()
        ),
    )
    assert validate_layers(service_a, registry=_registry()) == ()
    assert validate_layers(service_b, registry=_registry()) == ()
    effective_b = resolve_configuration(
        service_b,
        registry=_registry(),
        distribution_state=DistributionState.DISTRIBUTING,
    )
    assert effective_b.provider_instances == ()
    assert effective_b.provider_model_routes == ()


def test_workspace_rejects_provider_resources_and_inherited_disables() -> None:
    workspace = RevisionLayer(
        ConfigurationScope(service_id=SERVICE_A, workspace_id=WORKSPACE),
        "workspace",
        ScopeConfiguration(
            provider_instances=_global_content().provider_instances,
            inherited_disables=(
                InheritedDisable(ResourceKind.PROVIDER_MODEL_ROUTE, ROUTE_A),
            ),
        ),
    )
    issues = validate_layers(
        (
            RevisionLayer(ConfigurationScope(), "global", _global_content()),
            RevisionLayer(
                ConfigurationScope(service_id=SERVICE_A),
                "service",
                ScopeConfiguration(),
            ),
            workspace,
        ),
        registry=_registry(),
    )
    assert any("assignments only" in issue.reason for issue in issues)


def test_validation_reports_cycle_schema_capability_and_safe_paths() -> None:
    invalid = replace(
        _global_content(),
        provider_model_routes=(
            replace(
                _global_content().provider_model_routes[0],
                capabilities=frozenset({"image"}),
            ),
        ),
    )
    layers = (
        RevisionLayer(ConfigurationScope(), "global", invalid),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A), "service-a", ScopeConfiguration()
        ),
        RevisionLayer(
            ConfigurationScope(service_id=SERVICE_A), "cycle", ScopeConfiguration()
        ),
    )
    issues = validate_layers(layers, registry=_registry())
    assert any("cycle" in item.reason for item in issues)
    assert any(item.field_path.endswith("capabilities") for item in issues)
    assert all(CREDENTIAL not in item.reason for item in issues)


def test_closed_schema_and_endpoint_trust_reject_invalid_values() -> None:
    registry = _registry()
    issues = registry.validate(
        _document("provider.settings", {"region": "eu", "token": "hidden"}),
        field_path="settings",
    )
    assert {item.field_path for item in issues} == {"settings.document.token"}
    assert validate_endpoint("http://provider.example", field_path="endpoint")
    assert validate_endpoint("http://127.0.0.1:8000", field_path="endpoint") == ()


def test_registered_document_is_deeply_immutable_and_finds_nested_secrets() -> None:
    values: list[object] = ["one"]
    inner: dict[str, object] = {"token": "hidden", "values": values}
    nested: dict[str, object] = {"nested": inner}
    document = _document("nested.settings", nested)
    registry = SettingsSchemaRegistry(
        (RegisteredSchema("nested.settings", 1, {"nested": dict}),)
    )
    inner["token"] = "changed"  # noqa: S105
    values.append("two")
    assert document.document["nested"]["token"] == "hidden"  # noqa: S105
    assert document.document["nested"]["values"] == ("one",)
    issues = registry.validate(document, field_path="settings")
    assert {item.field_path for item in issues} == {"settings.document.nested.token"}


def test_route_price_policy_rejects_invalid_authority_and_time_values() -> None:
    """Reject incomplete source authority, invalid cron, and unsafe stale ages."""
    with pytest.raises(ValueError, match="lookup identifier"):
        PriceAuthority(PriceAuthorityMode.SOURCE, "catalog-test")
    route = _global_content().provider_model_routes[0]
    with pytest.raises(ValueError, match="cron"):
        replace(route, synchronization_schedule="each week")
    with pytest.raises(ValueError, match="cron"):
        replace(route, synchronization_schedule="99 0 * * 0")
    with pytest.raises(ValueError, match="one second"):
        replace(route, stale_after_seconds=0)
    manual = replace(
        route,
        price_authority=PriceAuthority(PriceAuthorityMode.MANUAL),
        synchronization_state=SynchronizationState.STALE,
    )
    assert manual.synchronization_state is SynchronizationState.MANUAL

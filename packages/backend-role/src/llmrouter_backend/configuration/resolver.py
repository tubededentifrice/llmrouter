"""Deterministic configuration inheritance and validation."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from .errors import ValidationIssue
from .model import (
    Assignment,
    CatalogKind,
    ConfigurationState,
    EffectiveConfiguration,
    EffectiveItem,
    ProviderInstance,
    ProviderModelRoute,
    ResourceKind,
)

if TYPE_CHECKING:
    from .model import DistributionState, RevisionLayer
    from .schema import SettingsSchemaRegistry

_ASSIGNMENT_NAME = re.compile(r"^[a-z][a-z0-9.-]{0,99}$")
_MAXIMUM_ITEMS_PER_KIND = 10_000
_MINIMUM_LAYER_COUNT = 2
_MAXIMUM_EMBEDDING_DIMENSIONS = 4_096
_MAXIMUM_ASSIGNMENT_CANDIDATES = 8
_MINIMUM_ATTEMPT_TIMEOUT_MS = 100
_MAXIMUM_ATTEMPT_TIMEOUT_MS = 120_000


def resolve_configuration(  # noqa: C901, PLR0912
    layers: tuple[RevisionLayer, ...],
    *,
    registry: SettingsSchemaRegistry,
    distribution_state: DistributionState,
) -> EffectiveConfiguration:
    """Resolve ordered global, service, and optional workspace layers."""
    issues = validate_layers(layers, registry=registry)
    if issues:
        msg = "The configuration layers are invalid."
        raise ValueError(msg)
    service_layers = tuple(
        layer.scope.service_id
        for layer in layers
        if layer.scope.service_id is not None and layer.scope.workspace_id is None
    )
    service_id = service_layers[-1]
    workspace_id = layers[-1].scope.workspace_id
    target_source = layers[-1].scope.source_layer

    catalog: dict[str, EffectiveItem] = {}
    instances: dict[str, EffectiveItem] = {}
    routes: dict[str, EffectiveItem] = {}
    assignments: dict[str, EffectiveItem] = {}
    disabled_instances: set[str] = set()
    disabled_routes: set[str] = set()

    for layer in layers:
        source = layer.scope.source_layer
        for catalog_item in layer.content.catalog:
            catalog[catalog_item.stable_id] = EffectiveItem(
                stable_id=catalog_item.stable_id,
                owner_scope="global",
                source_layer=source,
                state=catalog_item.state,
                inherited=source != target_source,
                active_revision=layer.revision_id,
                value=catalog_item,
            )
        for disabled in layer.content.inherited_disables:
            if disabled.resource_kind is ResourceKind.PROVIDER_INSTANCE:
                disabled_instances.add(disabled.resource_id)
            else:
                disabled_routes.add(disabled.resource_id)
        for instance_item in layer.content.provider_instances:
            if _eligible(
                instance_item.eligible_service_ids,
                service_layers,
                owner_service_id=layer.scope.service_id,
            ):
                instances[instance_item.provider_instance_id] = _effective_resource(
                    instance_item,
                    stable_id=instance_item.provider_instance_id,
                    layer=layer,
                    target_source=target_source,
                    disabled=instance_item.provider_instance_id in disabled_instances,
                )
        for route_item in layer.content.provider_model_routes:
            if _eligible(
                route_item.eligible_service_ids,
                service_layers,
                owner_service_id=layer.scope.service_id,
            ):
                routes[route_item.provider_model_route_id] = _effective_resource(
                    route_item,
                    stable_id=route_item.provider_model_route_id,
                    layer=layer,
                    target_source=target_source,
                    disabled=route_item.provider_model_route_id in disabled_routes,
                )
        for assignment_item in layer.content.assignments:
            assignments[assignment_item.name] = EffectiveItem(
                stable_id=assignment_item.name,
                owner_scope=layer.scope.kind,
                source_layer=source,
                state=assignment_item.state,
                inherited=source != target_source,
                active_revision=layer.revision_id,
                value=assignment_item,
            )

    for stable_id in disabled_instances:
        effective_item = instances.get(stable_id)
        if effective_item is not None:
            instances[stable_id] = replace(
                effective_item, state=ConfigurationState.DISABLED
            )
    for stable_id in disabled_routes:
        effective_item = routes.get(stable_id)
        if effective_item is not None:
            routes[stable_id] = replace(
                effective_item, state=ConfigurationState.DISABLED
            )

    active_revision = layers[-1].revision_id
    return EffectiveConfiguration(
        service_id=service_id,
        workspace_id=workspace_id,
        active_revision=active_revision,
        distribution_state=distribution_state,
        catalog=_sorted_values(catalog),
        provider_instances=_sorted_values(instances),
        provider_model_routes=_sorted_values(routes),
        assignments=_sorted_values(assignments),
    )


def validate_layers(  # noqa: C901, PLR0912, PLR0915
    layers: tuple[RevisionLayer, ...], *, registry: SettingsSchemaRegistry
) -> tuple[ValidationIssue, ...]:
    """Validate complete effective content without reading hidden identities."""
    issues: list[ValidationIssue] = []
    if not layers or layers[0].scope.kind != "global":
        return (ValidationIssue("scope", "The ordered scope chain is incomplete."),)
    if layers[-1].scope.kind != "global" and len(layers) < _MINIMUM_LAYER_COUNT:
        return (ValidationIssue("scope", "The ordered scope chain is incomplete."),)
    seen_service_ids: set[str] = set()
    all_catalog: dict[str, object] = {}
    all_instances: dict[str, ProviderInstance] = {}
    instances: dict[str, ProviderInstance] = {}
    routes: dict[str, ProviderModelRoute] = {}
    disabled_instances: set[str] = set()
    disabled_routes: set[str] = set()
    effective_assignments: dict[str, tuple[Assignment, str]] = {}
    target_service_layers = tuple(
        layer.scope.service_id
        for layer in layers
        if layer.scope.service_id is not None and layer.scope.workspace_id is None
    )

    for layer_index, layer in enumerate(layers):
        path = f"layers[{layer_index}]"
        if layer.scope.kind == "service":
            service_id = layer.scope.service_id
            if service_id is None or service_id in seen_service_ids:
                issues.append(
                    ValidationIssue(
                        f"{path}.scope", "The service chain contains a cycle."
                    )
                )
            else:
                seen_service_ids.add(service_id)
        if layer.scope.kind == "workspace" and layer is not layers[-1]:
            issues.append(
                ValidationIssue(f"{path}.scope", "A workspace must be the last layer.")
            )
        if layer.scope.kind != "global" and layer.content.catalog:
            issues.append(
                ValidationIssue(
                    f"{path}.catalog", "Only the global layer can own catalog entries."
                )
            )
        if layer.scope.kind == "workspace" and (
            layer.content.provider_instances
            or layer.content.provider_model_routes
            or layer.content.inherited_disables
        ):
            issues.append(
                ValidationIssue(
                    path,
                    "A workspace can contain assignments only in this release.",
                )
            )
        issues.extend(_unique_issues(layer, path))
        for index, item in enumerate(layer.content.catalog):
            all_catalog[item.stable_id] = item
            if item.kind is CatalogKind.PROVIDER and item.settings is None:
                issues.append(
                    ValidationIssue(
                        f"{path}.catalog[{index}].settings",
                        "A provider catalog entry must name its settings schema.",
                    )
                )
            if item.settings is not None:
                issues.extend(
                    registry.validate(
                        item.settings, field_path=f"{path}.catalog[{index}].settings"
                    )
                )
        for disabled in layer.content.inherited_disables:
            target = (
                instances
                if disabled.resource_kind is ResourceKind.PROVIDER_INSTANCE
                else routes
            )
            if disabled.resource_id not in target:
                issues.append(
                    ValidationIssue(
                        f"{path}.inherited_disables",
                        "The inherited resource is unknown or not eligible.",
                    )
                )
            if disabled.resource_kind is ResourceKind.PROVIDER_INSTANCE:
                disabled_instances.add(disabled.resource_id)
            else:
                disabled_routes.add(disabled.resource_id)
        for index, instance_item in enumerate(layer.content.provider_instances):
            item_path = f"{path}.provider_instances[{index}]"
            catalog = all_catalog.get(instance_item.provider_catalog_id)
            if (
                catalog is None
                or getattr(catalog, "kind", None) is not CatalogKind.PROVIDER
            ):
                issues.append(
                    ValidationIssue(
                        f"{item_path}.provider_catalog_id",
                        "The provider catalog reference is unknown.",
                    )
                )
            elif (
                instance_item.state is ConfigurationState.ACTIVE
                and getattr(catalog, "state", None) is not ConfigurationState.ACTIVE
            ):
                issues.append(
                    ValidationIssue(
                        f"{item_path}.provider_catalog_id",
                        "The provider catalog entry is not active.",
                    )
                )
            issues.extend(
                registry.validate(
                    instance_item.settings, field_path=f"{item_path}.settings"
                )
            )
            if _eligible(
                instance_item.eligible_service_ids,
                target_service_layers,
                owner_service_id=layer.scope.service_id,
            ):
                instances[instance_item.provider_instance_id] = instance_item
            all_instances[instance_item.provider_instance_id] = instance_item
        for index, route_item in enumerate(layer.content.provider_model_routes):
            item_path = f"{path}.provider_model_routes[{index}]"
            instance = all_instances.get(route_item.provider_instance_id)
            model = all_catalog.get(route_item.canonical_model_id)
            if instance is None:
                issues.append(
                    ValidationIssue(
                        f"{item_path}.provider_instance_id",
                        "The provider instance is unknown or not eligible.",
                    )
                )
            elif (
                route_item.state is ConfigurationState.ACTIVE
                and instance.state is not ConfigurationState.ACTIVE
            ):
                issues.append(
                    ValidationIssue(
                        f"{item_path}.provider_instance_id",
                        "The provider instance is not active.",
                    )
                )
            if model is None or getattr(model, "kind", None) is not CatalogKind.MODEL:
                issues.append(
                    ValidationIssue(
                        f"{item_path}.canonical_model_id",
                        "The canonical model is unknown.",
                    )
                )
            elif (
                route_item.state is ConfigurationState.ACTIVE
                and getattr(model, "state", None) is not ConfigurationState.ACTIVE
            ):
                issues.append(
                    ValidationIssue(
                        f"{item_path}.canonical_model_id",
                        "The canonical model is not active.",
                    )
                )
            else:
                model_capabilities: frozenset[str] = getattr(
                    model, "capabilities", frozenset()
                )
                if not route_item.capabilities <= model_capabilities:
                    issues.append(
                        ValidationIssue(
                            f"{item_path}.capabilities",
                            "The route capability does not match the canonical model.",
                        )
                    )
            if "embedding" in route_item.capabilities and (
                route_item.embedding_model_space_id is None
                or route_item.embedding_dimensions is None
                or not 1
                <= route_item.embedding_dimensions
                <= _MAXIMUM_EMBEDDING_DIMENSIONS
            ):
                issues.append(
                    ValidationIssue(
                        item_path,
                        "An embedding route needs a model space and valid dimensions.",
                    )
                )
            issues.extend(
                registry.validate(
                    route_item.settings, field_path=f"{item_path}.settings"
                )
            )
            if _eligible(
                route_item.eligible_service_ids,
                target_service_layers,
                owner_service_id=layer.scope.service_id,
            ):
                if route_item.provider_instance_id in instances:
                    routes[route_item.provider_model_route_id] = route_item
                else:
                    issues.append(
                        ValidationIssue(
                            f"{item_path}.provider_instance_id",
                            "The provider instance is unknown or not eligible.",
                        )
                    )
        for index, assignment in enumerate(layer.content.assignments):
            effective_assignments[assignment.name] = (
                assignment,
                f"{path}.assignments[{index}]",
            )
    for assignment, assignment_path in effective_assignments.values():
        issues.extend(
            _assignment_issues(
                assignment,
                path=assignment_path,
                routes=routes,
                disabled_routes=disabled_routes,
                instances=instances,
                disabled_instances=disabled_instances,
            )
        )
    if any(
        len(items) > _MAXIMUM_ITEMS_PER_KIND
        for layer in layers
        for items in (
            layer.content.catalog,
            layer.content.provider_instances,
            layer.content.provider_model_routes,
            layer.content.assignments,
        )
    ):
        issues.append(
            ValidationIssue(
                "configuration", "The configuration exceeds the item limit."
            )
        )
    return tuple(issues)


def _assignment_issues(  # noqa: PLR0913
    assignment: Assignment,
    *,
    path: str,
    routes: dict[str, ProviderModelRoute],
    disabled_routes: set[str],
    instances: dict[str, ProviderInstance],
    disabled_instances: set[str],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if not _ASSIGNMENT_NAME.fullmatch(assignment.name):
        issues.append(
            ValidationIssue(f"{path}.name", "The assignment name is invalid.")
        )
    if not 1 <= len(assignment.candidates) <= _MAXIMUM_ASSIGNMENT_CANDIDATES:
        issues.append(
            ValidationIssue(
                f"{path}.candidates",
                "An assignment must contain from one to eight candidates.",
            )
        )
    candidate_ids = [item.provider_model_route_id for item in assignment.candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        issues.append(
            ValidationIssue(f"{path}.candidates", "A route occurs more than one time.")
        )
    for index, candidate in enumerate(assignment.candidates):
        candidate_path = f"{path}.candidates[{index}]"
        route = routes.get(candidate.provider_model_route_id)
        if route is None:
            issues.append(
                ValidationIssue(
                    f"{candidate_path}.provider_model_route_id",
                    "The route is unknown or not eligible.",
                )
            )
            continue
        if assignment.state is ConfigurationState.ACTIVE and (
            route.state is not ConfigurationState.ACTIVE
            or candidate.provider_model_route_id in disabled_routes
        ):
            issues.append(
                ValidationIssue(
                    f"{candidate_path}.provider_model_route_id",
                    "The route is not eligible for new work.",
                )
            )
            continue
        instance = instances.get(route.provider_instance_id)
        if assignment.state is ConfigurationState.ACTIVE and (
            instance is None
            or instance.state is not ConfigurationState.ACTIVE
            or instance.provider_instance_id in disabled_instances
        ):
            issues.append(
                ValidationIssue(
                    f"{candidate_path}.provider_model_route_id",
                    "The provider instance is not eligible for new work.",
                )
            )
        if not assignment.required_capabilities <= route.capabilities:
            issues.append(
                ValidationIssue(
                    f"{candidate_path}.provider_model_route_id",
                    "The route does not supply the assignment capability.",
                )
            )
        if not (
            _MINIMUM_ATTEMPT_TIMEOUT_MS
            <= candidate.attempt_timeout_ms
            <= _MAXIMUM_ATTEMPT_TIMEOUT_MS
        ):
            issues.append(
                ValidationIssue(
                    f"{candidate_path}.attempt_timeout_ms",
                    "The attempt timeout is outside the accepted range.",
                )
            )
    return tuple(issues)


def _unique_issues(layer: RevisionLayer, path: str) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    groups = (
        ("catalog", [item.stable_id for item in layer.content.catalog]),
        (
            "provider_instances",
            [item.provider_instance_id for item in layer.content.provider_instances],
        ),
        (
            "provider_model_routes",
            [
                item.provider_model_route_id
                for item in layer.content.provider_model_routes
            ],
        ),
        ("assignments", [item.name for item in layer.content.assignments]),
        (
            "inherited_disables",
            [
                (item.resource_kind, item.resource_id)
                for item in layer.content.inherited_disables
            ],
        ),
    )
    for name, identities in groups:
        if len(identities) != len(set(identities)):
            issues.append(
                ValidationIssue(
                    f"{path}.{name}", "A stable identity occurs more than one time."
                )
            )
    return tuple(issues)


def _effective_resource(
    value: ProviderInstance | ProviderModelRoute,
    *,
    stable_id: str,
    layer: RevisionLayer,
    target_source: str,
    disabled: bool,
) -> EffectiveItem:
    state = ConfigurationState.DISABLED if disabled else value.state
    return EffectiveItem(
        stable_id=stable_id,
        owner_scope="global" if layer.scope.kind == "global" else "service",
        source_layer=layer.scope.source_layer,
        state=state,
        inherited=layer.scope.source_layer != target_source,
        active_revision=layer.revision_id,
        value=value,
    )


def _eligible(
    eligible_ids: frozenset[str],
    service_layers: tuple[str, ...],
    *,
    owner_service_id: str | None,
) -> bool:
    """Apply permission roots only at or below the owning service."""
    if not service_layers or not eligible_ids:
        return True
    if owner_service_id is None:
        permitted_chain = service_layers
    else:
        try:
            owner_index = service_layers.index(owner_service_id)
        except ValueError:
            return False
        if owner_index == len(service_layers) - 1:
            return True
        permitted_chain = service_layers[owner_index + 1 :]
    return bool(eligible_ids.intersection(permitted_chain))


def _sorted_values(values: dict[str, EffectiveItem]) -> tuple[EffectiveItem, ...]:
    return tuple(values[key] for key in sorted(values))

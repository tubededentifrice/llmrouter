"""Closed configuration values and effective results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ConfigurationState(StrEnum):
    """The accepted state of one configuration item."""

    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class CatalogKind(StrEnum):
    """Shared catalog entry kinds."""

    PROVIDER = "provider"
    MODEL = "model"


class ResourceKind(StrEnum):
    """Resource kinds that a child can disable."""

    PROVIDER_INSTANCE = "provider_instance"
    PROVIDER_MODEL_ROUTE = "provider_model_route"


class DistributionState(StrEnum):
    """Normal configuration distribution states."""

    DISTRIBUTING = "distributing"
    CURRENT = "current"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ConfigurationScope:
    """One global, service, or workspace configuration layer."""

    service_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        """Reject a workspace without its service."""
        if self.workspace_id is not None and self.service_id is None:
            msg = "A workspace configuration scope must include its service."
            raise ValueError(msg)

    @property
    def kind(self) -> str:
        """Return the database scope kind."""
        if self.workspace_id is not None:
            return "workspace"
        return "service" if self.service_id is not None else "global"

    @property
    def source_layer(self) -> str:
        """Return the public source layer identity."""
        if self.workspace_id is not None:
            return self.workspace_id
        return self.service_id or "global"


@dataclass(frozen=True, slots=True)
class RegisteredDocument:
    """One closed document that names its registered schema."""

    schema_name: str
    major_version: int
    document: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Copy the document so callers cannot change it after validation."""
        if not self.schema_name or self.major_version < 1:
            msg = "A registered document must name a schema and positive major version."
            raise ValueError(msg)
        object.__setattr__(self, "document", _freeze_mapping(self.document))


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One shared provider or model catalog entry."""

    kind: CatalogKind
    stable_id: str
    display_name: str
    capabilities: frozenset[str]
    state: ConfigurationState = ConfigurationState.ACTIVE
    settings: RegisteredDocument | None = None


@dataclass(frozen=True, slots=True)
class ProviderInstance:
    """One global or service-owned provider instance."""

    provider_instance_id: str
    provider_catalog_id: str
    display_name: str
    endpoint: str
    credential_id: str
    settings: RegisteredDocument
    state: ConfigurationState = ConfigurationState.ACTIVE
    eligible_service_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ProviderModelRoute:
    """One global or service-owned route to a canonical model."""

    provider_model_route_id: str
    provider_instance_id: str
    canonical_model_id: str
    wire_model: str
    capabilities: frozenset[str]
    settings: RegisteredDocument
    state: ConfigurationState = ConfigurationState.ACTIVE
    eligible_service_ids: frozenset[str] = frozenset()
    embedding_model_space_id: str | None = None
    embedding_dimensions: int | None = None


@dataclass(frozen=True, slots=True)
class AssignmentCandidate:
    """One ordered candidate in a complete fallback chain."""

    provider_model_route_id: str
    attempt_timeout_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class Assignment:
    """One complete named fallback chain at one layer."""

    name: str
    candidates: tuple[AssignmentCandidate, ...]
    required_capabilities: frozenset[str] = frozenset()
    state: ConfigurationState = ConfigurationState.ACTIVE


@dataclass(frozen=True, slots=True)
class InheritedDisable:
    """One local disable of an inherited provider resource."""

    resource_kind: ResourceKind
    resource_id: str


@dataclass(frozen=True, slots=True)
class ScopeConfiguration:
    """The complete local content of one immutable scope revision."""

    catalog: tuple[CatalogEntry, ...] = ()
    provider_instances: tuple[ProviderInstance, ...] = ()
    provider_model_routes: tuple[ProviderModelRoute, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    inherited_disables: tuple[InheritedDisable, ...] = ()


@dataclass(frozen=True, slots=True)
class RevisionLayer:
    """One active layer supplied to the deterministic resolver."""

    scope: ConfigurationScope
    revision_id: str
    content: ScopeConfiguration


@dataclass(frozen=True, slots=True)
class EffectiveItem:
    """One resolved configuration item with its provenance."""

    stable_id: str
    owner_scope: str
    source_layer: str
    state: ConfigurationState
    inherited: bool
    active_revision: str
    value: object


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """One deterministic effective configuration snapshot."""

    service_id: str
    workspace_id: str | None
    active_revision: str
    distribution_state: DistributionState
    catalog: tuple[EffectiveItem, ...]
    provider_instances: tuple[EffectiveItem, ...]
    provider_model_routes: tuple[EffectiveItem, ...]
    assignments: tuple[EffectiveItem, ...]


@dataclass(frozen=True, slots=True)
class ConfigurationWriteResult:
    """The public result of one atomic publication."""

    resource_id: str
    active_revision: str
    distribution_state: DistributionState
    operation_id: str

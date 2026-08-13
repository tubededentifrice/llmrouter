"""Closed configuration values and effective results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from llmrouter_backend.accounting.model import PriceComponent, SynchronizationState

_MAXIMUM_SOURCE_NAME_CHARACTERS = 100
_MAXIMUM_LOOKUP_IDENTIFIER_CHARACTERS = 500
_CRON_FIELD_COUNT = 5
_MAXIMUM_SCHEDULE_CHARACTERS = 100
_MAXIMUM_STALE_AFTER_SECONDS = 365 * 24 * 60 * 60
_MAXIMUM_PRICE_COMPONENTS = 32
_CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


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


class PriceAuthorityMode(StrEnum):
    """The accepted authority modes for one route price."""

    MANUAL = "manual"
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class PriceAuthority:
    """One explicit manual pin or named synchronization source."""

    mode: PriceAuthorityMode
    source_name: str | None = None
    lookup_identifier: str | None = None

    def __post_init__(self) -> None:
        """Require complete and bounded authority values."""
        if self.mode is PriceAuthorityMode.MANUAL:
            if self.source_name is not None or self.lookup_identifier is not None:
                msg = "A manual price authority must not contain source values."
                raise ValueError(msg)
            return
        if (
            self.source_name is None
            or not self.source_name
            or len(self.source_name) > _MAXIMUM_SOURCE_NAME_CHARACTERS
            or not self.source_name[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in self.source_name
            )
        ):
            msg = "A source price authority must name a valid source."
            raise ValueError(msg)
        if (
            self.lookup_identifier is None
            or not 1
            <= len(self.lookup_identifier)
            <= _MAXIMUM_LOOKUP_IDENTIFIER_CHARACTERS
        ):
            msg = "A source price authority must contain a lookup identifier."
            raise ValueError(msg)


_WEEKLY_PRICE_SYNCHRONIZATION = "0 0 * * 0"
_DEFAULT_PRICE_STALE_AFTER_SECONDS = 14 * 24 * 60 * 60


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
    price_authority: PriceAuthority
    prices: tuple[PriceComponent, ...]
    synchronization_schedule: str = _WEEKLY_PRICE_SYNCHRONIZATION
    stale_after_seconds: int = _DEFAULT_PRICE_STALE_AFTER_SECONDS
    price_version: str | None = None
    synchronization_state: SynchronizationState | None = None
    state: ConfigurationState = ConfigurationState.ACTIVE
    eligible_service_ids: frozenset[str] = frozenset()
    embedding_model_space_id: str | None = None
    embedding_dimensions: int | None = None

    def __post_init__(self) -> None:
        """Freeze and validate exact public route prices and policy."""
        prices = tuple(sorted(self.prices, key=lambda item: item.unit.value))
        legacy_empty_price = not prices and (
            (
                self.price_authority.mode is PriceAuthorityMode.SOURCE
                and self.synchronization_state
                in (None, SynchronizationState.MISSING, SynchronizationState.FAILED)
            )
            or (
                self.price_authority.mode is PriceAuthorityMode.MANUAL
                and self.synchronization_state is SynchronizationState.MANUAL
            )
        )
        if not prices and not legacy_empty_price:
            msg = "A provider-model route must contain one or more prices."
            raise ValueError(msg)
        if len({item.unit for item in prices}) != len(prices):
            msg = "A provider-model route price unit must be unique."
            raise ValueError(msg)
        if len(prices) > _MAXIMUM_PRICE_COMPONENTS:
            msg = "A provider-model route contains too many price components."
            raise ValueError(msg)
        if prices and len({item.currency for item in prices}) != 1:
            msg = "A provider-model route must use one accounting currency."
            raise ValueError(msg)
        object.__setattr__(self, "prices", prices)
        if (
            not _valid_cron(self.synchronization_schedule)
            or len(self.synchronization_schedule) > _MAXIMUM_SCHEDULE_CHARACTERS
        ):
            msg = "A price synchronization schedule must be a bounded cron value."
            raise ValueError(msg)
        if not 1 <= self.stale_after_seconds <= _MAXIMUM_STALE_AFTER_SECONDS:
            msg = "A price stale threshold must be from one second to one year."
            raise ValueError(msg)
        expected_state = (
            SynchronizationState.MANUAL
            if self.price_authority.mode is PriceAuthorityMode.MANUAL
            else (
                SynchronizationState.MISSING
                if not prices
                else SynchronizationState.CURRENT
            )
        )
        if (
            self.synchronization_state is None
            or self.price_authority.mode is PriceAuthorityMode.MANUAL
        ):
            object.__setattr__(self, "synchronization_state", expected_state)


def _valid_cron(value: str) -> bool:
    fields = value.split()
    if len(fields) != _CRON_FIELD_COUNT:
        return False
    return all(
        _valid_cron_field(field, minimum, maximum)
        for field, (minimum, maximum) in zip(fields, _CRON_LIMITS, strict=True)
    )


def _valid_cron_field(value: str, minimum: int, maximum: int) -> bool:
    for item in value.split(","):
        base, separator, step_value = item.partition("/")
        if separator and (
            not step_value.isdecimal() or not 1 <= int(step_value) <= maximum
        ):
            return False
        if base == "*":
            continue
        start_value, range_separator, end_value = base.partition("-")
        if not start_value.isdecimal():
            return False
        start = int(start_value)
        if not minimum <= start <= maximum:
            return False
        if range_separator and (
            not end_value.isdecimal() or not start <= int(end_value) <= maximum
        ):
            return False
    return True


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

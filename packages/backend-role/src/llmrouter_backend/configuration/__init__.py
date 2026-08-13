"""Catalog, inheritance, validation, and immutable configuration revisions."""

from .errors import ConfigurationError, ConfigurationErrorCode, ValidationIssue
from .model import (
    Assignment,
    AssignmentCandidate,
    CatalogEntry,
    CatalogKind,
    ConfigurationScope,
    ConfigurationState,
    ConfigurationWriteResult,
    DistributionState,
    EffectiveConfiguration,
    EffectiveItem,
    InheritedDisable,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    ResourceKind,
    RevisionLayer,
    ScopeConfiguration,
)
from .repository import PostgresConfigurationRepository
from .resolver import resolve_configuration, validate_layers
from .schema import RegisteredSchema, SettingsSchemaRegistry, validate_endpoint

__all__ = [
    "Assignment",
    "AssignmentCandidate",
    "CatalogEntry",
    "CatalogKind",
    "ConfigurationError",
    "ConfigurationErrorCode",
    "ConfigurationScope",
    "ConfigurationState",
    "ConfigurationWriteResult",
    "DistributionState",
    "EffectiveConfiguration",
    "EffectiveItem",
    "InheritedDisable",
    "PostgresConfigurationRepository",
    "ProviderInstance",
    "ProviderModelRoute",
    "RegisteredDocument",
    "RegisteredSchema",
    "ResourceKind",
    "RevisionLayer",
    "ScopeConfiguration",
    "SettingsSchemaRegistry",
    "ValidationIssue",
    "resolve_configuration",
    "validate_endpoint",
    "validate_layers",
]

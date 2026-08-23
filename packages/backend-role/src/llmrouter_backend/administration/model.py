"""Closed documents for the basic administration HTTP surface."""
# ruff: noqa: TC001

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmrouter_backend.accounting import UsageUnit
from llmrouter_backend.authority import Audience
from llmrouter_backend.budgets import ResetPeriod
from llmrouter_backend.configuration import ConfigurationState, PriceAuthorityMode
from llmrouter_backend.machine_identity import WorkspaceLimit

OpaqueId = Annotated[str, Field(min_length=1, max_length=200)]
Reason = Annotated[str, Field(min_length=1, max_length=500)]
UuidV7 = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


class ClosedAdministrationModel(BaseModel):
    """Reject unknown administration fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RegisteredDocumentInput(ClosedAdministrationModel):
    """One closed registered settings document."""

    schema_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9._-]{0,99}$")]
    major_version: Annotated[int, Field(ge=1)]
    document: dict[str, Any] = Field(default_factory=dict)


class CredentialCreateInput(ClosedAdministrationModel):
    """One write-only provider credential input."""

    owner_scope: OpaqueId
    provider_catalog_id: OpaqueId
    secret: Annotated[str, Field(min_length=1, max_length=65_536, repr=False)]
    safe_label: Annotated[str, Field(max_length=200)] | None = None

    def __repr__(self) -> str:
        """Do not expose credential material."""
        return "CredentialCreateInput([REDACTED])"

    __str__ = __repr__


class CredentialChangeInput(ClosedAdministrationModel):
    """One revision-safe credential change."""

    expected_revision: OpaqueId
    reason: Reason
    replacement_secret: str | None = Field(
        default=None, min_length=1, max_length=65_536, repr=False
    )

    def __repr__(self) -> str:
        """Do not expose replacement credential material."""
        return "CredentialChangeInput([REDACTED])"

    __str__ = __repr__


class ProviderInstanceInput(ClosedAdministrationModel):
    """One complete OpenRouter provider-instance replacement."""

    provider_catalog_id: OpaqueId
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    endpoint: Annotated[str, Field(min_length=1, max_length=2_048)]
    credential_id: OpaqueId
    state: ConfigurationState
    settings: RegisteredDocumentInput
    expected_revision: OpaqueId | None
    reason: Reason
    eligible_service_ids: list[OpaqueId] = Field(default_factory=list, max_length=1_000)


class PriceAuthorityInput(ClosedAdministrationModel):
    """One manual or synchronized route price authority."""

    mode: PriceAuthorityMode
    source_name: Annotated[str, Field(max_length=100)] | None = None
    lookup_identifier: Annotated[str, Field(max_length=500)] | None = None


class PriceComponentInput(ClosedAdministrationModel):
    """One exact typed price component."""

    unit: UsageUnit
    price: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    raw_source_value: Annotated[str, Field(min_length=1, max_length=200)]
    unit_quantity: Annotated[
        str, Field(pattern=r"^([1-9][0-9]*)(\.[0-9]+)?$|^0\.[0-9]*[1-9][0-9]*$")
    ]


class ProviderModelRouteInput(ClosedAdministrationModel):
    """One complete provider-model route replacement."""

    provider_instance_id: OpaqueId
    canonical_model_id: OpaqueId
    wire_model: Annotated[str, Field(min_length=1, max_length=500)]
    capabilities: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=32
    )
    settings: RegisteredDocumentInput
    price_authority: PriceAuthorityInput
    prices: list[PriceComponentInput] = Field(max_length=32)
    synchronization_schedule: Annotated[str, Field(min_length=9, max_length=100)] = (
        "0 0 * * 0"
    )
    stale_after_seconds: Annotated[int, Field(ge=1, le=31_536_000)] = 1_209_600
    state: ConfigurationState
    expected_revision: OpaqueId | None
    reason: Reason
    eligible_service_ids: list[OpaqueId] = Field(default_factory=list, max_length=1_000)


class AssignmentCandidateInput(ClosedAdministrationModel):
    """One ordered fallback candidate."""

    provider_model_route_id: OpaqueId
    attempt_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 30_000


class AssignmentInput(ClosedAdministrationModel):
    """One complete assignment fallback-chain replacement."""

    expected_revision: OpaqueId | None
    state: ConfigurationState
    candidates: list[AssignmentCandidateInput] = Field(min_length=1, max_length=8)
    required_capabilities: list[Annotated[str, Field(min_length=1, max_length=100)]] = (
        Field(default_factory=list, max_length=32)
    )
    reason: Reason


class BudgetLimitInput(ClosedAdministrationModel):
    """One exact selected-scope budget replacement."""

    hard_limit: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    warning_threshold: (
        Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")] | None
    ) = None
    reset_period: ResetPeriod
    expected_revision: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]


class DiagnosticRunInput(ClosedAdministrationModel):
    """One bounded exact-route administrator diagnostic."""

    request_id: UuidV7
    exact_route: OpaqueId
    reason: Reason


class ExportCreateInput(ClosedAdministrationModel):
    """Create one bounded protected export."""

    data_class: Literal["accounting", "audit", "configuration", "captured_content"]
    service_id: OpaqueId | None = None
    workspace_id: OpaqueId | None = None
    range_start: datetime = Field(alias="from")
    range_end: datetime = Field(alias="to")
    export_format: Literal["jsonl", "csv"] = Field(alias="format")

    @model_validator(mode="after")
    def valid_range_and_scope(self) -> ExportCreateInput:
        """Reject an unordered, long, or incomplete export scope."""
        if self.range_start.tzinfo is None or self.range_end.tzinfo is None:
            message = "Export times must include a time zone."
            raise ValueError(message)
        if not self.range_start < self.range_end:
            message = "The export time range is not ordered."
            raise ValueError(message)
        if self.range_end - self.range_start > timedelta(days=1):
            message = "The export time range exceeds one day."
            raise ValueError(message)
        if self.workspace_id is not None and self.service_id is None:
            message = "A workspace export needs a service identity."
            raise ValueError(message)
        return self


class ExportRedeemInput(ClosedAdministrationModel):
    """Redeem one protected export without rendering its token."""

    redemption_token: str = Field(min_length=43, max_length=200, repr=False)

    def __repr__(self) -> str:
        """Do not expose the one-use token."""
        return "ExportRedeemInput([REDACTED])"

    __str__ = __repr__


class ServiceStateDocument(ClosedAdministrationModel):
    """One safe service or workspace state document."""

    kind: Literal["service", "workspace"]
    service_id: OpaqueId
    workspace_id: OpaqueId | None = None
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    state: Literal["active", "disabled", "retired"]
    revision: OpaqueId
    parent_service_id: OpaqueId | None = None


class BootstrapScopeInput(ClosedAdministrationModel):
    """One exact maximum machine authority for a service bootstrap."""

    audiences: list[Audience] = Field(min_length=1)
    operations: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1
    )
    workspace_limit: WorkspaceLimit = WorkspaceLimit.ALL_SERVICE_WORKSPACES

    @field_validator("audiences", "operations")
    @classmethod
    def values_are_unique(cls, values: list[object]) -> list[object]:
        """Reject duplicate authority values instead of silently merging them."""
        if len(values) != len(set(values)):
            message = "Bootstrap scope values must be unique."
            raise ValueError(message)
        return values


class ServiceCreateInput(ClosedAdministrationModel):
    """Create one service and its initial bootstrap credential."""

    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    parent_service_id: OpaqueId | None
    bootstrap_scope: BootstrapScopeInput


class ServiceUpdateInput(ClosedAdministrationModel):
    """Replace one service display name and parent link."""

    expected_revision: OpaqueId
    reason: Reason
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    new_parent_service_id: OpaqueId | None


class ServiceActionInput(ClosedAdministrationModel):
    """Apply one revision-safe service lifecycle action."""

    expected_revision: OpaqueId
    reason: Reason

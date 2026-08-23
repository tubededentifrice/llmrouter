"""Closed HTTP models for service and administrator operations."""
# ruff: noqa: TC003

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ApiName = str
ProviderAdapter = Literal[
    "openai",
    "openai_compatible",
    "openrouter",
    "custom",
    "wavespeed",
    "ollama",
    "local_embeddings",
    "fake",
]
InputModality = Literal["text", "image"]
OutputModality = Literal[
    "text", "structured_json", "embedding", "image", "video", "audio"
]
ModelCapability = Literal["tool_calling", "streaming", "reasoning"]
ReasoningLevel = Literal["none", "low", "medium", "high"]
UsageUnit = Literal[
    "input_token",
    "output_token",
    "cached_input_token",
    "image",
    "video_second",
    "audio_second",
    "request",
    "provider_unit",
]


class ClosedModel(BaseModel):
    """Reject fields that the native contract does not define."""

    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(ClosedModel):
    """One service-owned workspace input."""

    api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    display_name: str = Field(min_length=1, max_length=200)


class ServiceCreate(WorkspaceCreate):
    """One global service input."""

    parent_service_api_name: str | None = Field(
        default=None, pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
    )


class ServiceUpdate(ClosedModel):
    """Mutable current service fields."""

    display_name: str = Field(min_length=1, max_length=200)
    parent_service_api_name: str | None = Field(
        default=None, pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
    )


class ServiceKeyCreate(ClosedModel):
    """One named direct service key input."""

    name: str = Field(min_length=1, max_length=200)


class AdministratorSessionStart(ClosedModel):
    """A safe application-local sign-in return target."""

    return_to: str = Field(min_length=1, max_length=1000)


class Service(BaseModel):
    """Current service response."""

    api_name: str
    display_name: str
    parent_service_api_name: str | None = None
    created_at: datetime


class Workspace(BaseModel):
    """Current workspace response."""

    api_name: str
    display_name: str
    created_at: datetime


class ServiceKey(BaseModel):
    """Service key metadata without secret material."""

    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


class AdministratorSession(BaseModel):
    """Current allowlisted administrator session."""

    subject: str
    display_name: str
    expires_at: datetime
    csrf_token: str


class PageInfo(BaseModel):
    """Bounded page position."""

    has_more: bool
    next_cursor: str | None = None


class ServicePage(BaseModel):
    """Service page."""

    items: list[Service]
    page: PageInfo


class WorkspacePage(BaseModel):
    """Workspace page."""

    items: list[Workspace]
    page: PageInfo


class ServiceKeyCreated(BaseModel):
    """One-time service key response."""

    key: ServiceKey
    secret: str


class ServiceKeyPage(BaseModel):
    """Service key page."""

    items: list[ServiceKey]
    page: PageInfo


class ProviderWrite(ClosedModel):
    """One complete provider connection value."""

    api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    display_name: str = Field(min_length=1, max_length=200)
    adapter: ProviderAdapter
    endpoint: str | None = Field(default=None, min_length=1, max_length=4096)
    credential_api_name: str | None = Field(
        default=None, pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
    )
    enabled: bool


class Provider(ProviderWrite):
    """One safe current provider connection."""

    created_at: datetime


class ProviderPage(ClosedModel):
    """Provider page."""

    items: list[Provider]
    page: PageInfo


class ModelConstraints(ClosedModel):
    """Bounded embedding and media limits."""

    embedding_dimensions: list[int] | None = Field(
        default=None, min_length=1, max_length=64
    )
    max_input_images: int | None = Field(default=None, ge=0, le=8)
    max_input_image_bytes: int | None = Field(default=None, ge=1, le=20 * 1024 * 1024)
    max_output_duration_seconds: int | None = Field(default=None, ge=1, le=86_400)


class ReasoningMapping(ClosedModel):
    """Map one common reasoning level to a provider value."""

    level: ReasoningLevel
    provider_value: str = Field(min_length=1, max_length=200)


class UnitPriceWrite(ClosedModel):
    """One fixed-decimal unit price."""

    unit: UsageUnit
    amount: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")


class Price(ClosedModel):
    """One current manual or catalog price."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_prices: list[UnitPriceWrite] = Field(min_length=1, max_length=16)
    source: str | None = Field(default=None, max_length=500)
    synchronized_at: datetime | None = None


class ModelWrite(ClosedModel):
    """One complete canonical model value."""

    api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    display_name: str = Field(min_length=1, max_length=200)
    input_modalities: list[InputModality] = Field(min_length=1, max_length=2)
    output_modalities: list[OutputModality] = Field(min_length=1, max_length=6)
    capabilities: list[ModelCapability] = Field(max_length=3)
    constraints: ModelConstraints | None = None
    price_source: str | None = Field(default=None, min_length=1, max_length=500)
    price_lookup_key: str | None = Field(default=None, min_length=1, max_length=500)
    manual_price: Price | None = None


class Model(ClosedModel):
    """One current canonical model."""

    api_name: str
    display_name: str
    input_modalities: list[InputModality]
    output_modalities: list[OutputModality]
    capabilities: list[ModelCapability]
    constraints: ModelConstraints | None = None
    price_source: str | None = None
    price_lookup_key: str | None = None
    current_price: Price | None = None
    created_at: datetime


class ModelPage(ClosedModel):
    """Canonical model page."""

    items: list[Model]
    page: PageInfo


class ProviderModelWrite(ClosedModel):
    """One complete provider-model mapping value."""

    api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    provider_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    model_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    provider_model_name: str = Field(min_length=1, max_length=500)
    enabled: bool
    input_modalities: list[InputModality] | None = Field(
        default=None, min_length=1, max_length=2
    )
    output_modalities: list[OutputModality] | None = Field(
        default=None, min_length=1, max_length=6
    )
    capabilities: list[ModelCapability] | None = Field(default=None, max_length=3)
    constraints: ModelConstraints | None = None
    reasoning_mappings: list[ReasoningMapping] | None = Field(
        default=None, max_length=4
    )
    price_source: str | None = Field(default=None, min_length=1, max_length=500)
    price_lookup_key: str | None = Field(default=None, min_length=1, max_length=500)
    manual_price: Price | None = None


class ProviderModel(ClosedModel):
    """One current expanded provider-model mapping."""

    api_name: str
    provider_api_name: str
    model_api_name: str
    provider_model_name: str
    enabled: bool
    input_modalities: list[InputModality]
    output_modalities: list[OutputModality]
    capabilities: list[ModelCapability]
    constraints: ModelConstraints | None = None
    reasoning_mappings: list[ReasoningMapping]
    price_source: str | None = None
    price_lookup_key: str | None = None
    effective_price: Price | None = None
    created_at: datetime


class ProviderModelPage(ClosedModel):
    """Provider-model page."""

    items: list[ProviderModel]
    page: PageInfo


class AvailableProviderModel(ClosedModel):
    """Service-safe enabled provider-model data."""

    api_name: str
    display_name: str
    input_modalities: list[InputModality]
    output_modalities: list[OutputModality]
    capabilities: list[ModelCapability]
    constraints: ModelConstraints | None = None
    effective_price: Price | None = None


class AvailableProviderModelPage(ClosedModel):
    """Service-safe provider-model page."""

    items: list[AvailableProviderModel]
    page: PageInfo


class CredentialWrite(ClosedModel):
    """Write-only credential input."""

    api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    secret: str = Field(min_length=1, max_length=10_000)


class Credential(ClosedModel):
    """Credential metadata without its encrypted control."""

    api_name: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{12}$")
    created_at: datetime
    updated_at: datetime


class CredentialPage(ClosedModel):
    """Credential metadata page."""

    items: list[Credential]
    page: PageInfo


class ModelImportPreviewRequest(ClosedModel):
    """Select one registered provider catalog."""

    provider_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ModelImportCandidate(ClosedModel):
    """One deterministic registered catalog entry."""

    catalog_key: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)
    provider_model_name: str = Field(min_length=1, max_length=500)
    input_modalities: list[InputModality]
    output_modalities: list[OutputModality]
    capabilities: list[ModelCapability]
    constraints: ModelConstraints | None = None


class ModelImportPreview(ClosedModel):
    """One no-write catalog preview."""

    provider_api_name: str
    candidates: list[ModelImportCandidate]


class ModelImportSelection(ClosedModel):
    """Name one catalog entry and its two new resource identities."""

    catalog_key: str = Field(min_length=1, max_length=500)
    model_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    provider_model_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ModelImportRequest(ClosedModel):
    """One bounded atomic selected import."""

    provider_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    selections: list[ModelImportSelection] = Field(min_length=1, max_length=1000)


class ModelImportResult(ClosedModel):
    """Resources created by one selected import."""

    models: list[Model]
    provider_models: list[ProviderModel]


class ActivityEvent(BaseModel):
    """Basic current activity record."""

    id: str
    actor_subject: str
    action: str
    resource_type: str
    service_api_name: str | None = None
    resource_api_name: str | None = None
    resource_id: str | None = None
    result: str
    occurred_at: datetime


class ActivityPage(BaseModel):
    """Activity page."""

    items: list[ActivityEvent]
    page: PageInfo


class UsageItem(ClosedModel):
    """One typed provider usage value."""

    unit: UsageUnit
    quantity: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")


class Usage(ClosedModel):
    """One complete typed usage and cost value."""

    units: list[UsageItem]
    cost: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class UnitPrice(ClosedModel):
    """One applied typed unit price."""

    unit: UsageUnit
    amount: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")


class AppliedPrice(ClosedModel):
    """One provider price snapshot used for an attempt."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_prices: list[UnitPrice] = Field(min_length=1)
    source: str | None = Field(default=None, max_length=500)
    synchronized_at: datetime | None = None


class SafeErrorDetails(ClosedModel):
    """Safe corrective details without model or control content."""

    field: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class SafeError(ClosedModel):
    """One stable provider-neutral error captured for an attempt."""

    code: Literal[
        "authentication_required",
        "permission_denied",
        "invalid_request",
        "not_found",
        "conflict",
        "assignment_cycle",
        "provider_unavailable",
        "upstream_failed",
        "content_unavailable",
        "rate_limited",
        "internal_error",
    ]
    message: str = Field(min_length=1, max_length=1000)
    details: SafeErrorDetails | None = None


class RequestAttempt(ClosedModel):
    """One provider attempt in a complete detailed log."""

    provider_model_api_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    outcome: Literal["succeeded", "failed"]
    started_at: datetime
    completed_at: datetime | None = None
    usage: Usage
    applied_prices: AppliedPrice
    error: SafeError | None = None


class RequestLogSummary(ClosedModel):
    """Bounded list data for one detailed request log."""

    id: str
    service_api_name: str
    workspace_api_name: str
    assignment_api_name: str | None = None
    provider_model_api_name: str | None = None
    kind: Literal["model", "embedding", "media"]
    outcome: Literal["succeeded", "failed"]
    tags: list[str] | None = None
    started_at: datetime


class LogMedia(ClosedModel):
    """One captured media item without an object-store identifier."""

    id: str
    media_type: str = Field(min_length=1, max_length=200)
    role: Literal["input", "output"]
    size_bytes: int = Field(ge=0)


class RequestLog(ClosedModel):
    """One complete best-effort detailed request log."""

    summary: RequestLogSummary
    request_json: str = Field(max_length=5_000_000)
    response_json: str | None = Field(default=None, max_length=5_000_000)
    attempts: list[RequestAttempt] = Field(max_length=16)
    media: list[LogMedia] | None = Field(default=None, max_length=16)


class RequestLogPage(ClosedModel):
    """One bounded detailed-log page."""

    items: list[RequestLogSummary]
    page: PageInfo


class LogRetentionSettings(ClosedModel):
    """One global whole-day diagnostic retention duration."""

    duration_days: int = Field(ge=1, le=30)


class HealthComponent(ClosedModel):
    """One small operator health component."""

    name: str = Field(min_length=1, max_length=200)
    status: Literal["healthy", "degraded", "unavailable"]
    message: str | None = Field(default=None, max_length=500)


class AdministratorHealth(ClosedModel):
    """Small global administrator health summary."""

    status: Literal["healthy", "degraded", "unavailable"]
    checked_at: datetime
    components: list[HealthComponent]

"""Closed HTTP models for service and administrator operations."""
# ruff: noqa: TC003

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ApiName = str
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

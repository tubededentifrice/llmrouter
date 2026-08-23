"""Closed HTTP models for service and administrator operations."""
# ruff: noqa: TC003

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

ApiName = str


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

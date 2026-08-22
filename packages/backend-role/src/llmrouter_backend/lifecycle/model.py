"""Closed values for service and workspace lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class LifecycleState(StrEnum):
    """The exact service and workspace states."""

    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class ServiceAction(StrEnum):
    """Service lifecycle changes that global administration can make."""

    DISABLE = "disable"
    RESTORE = "restore"
    RETIRE = "retire"


class WorkspaceAction(StrEnum):
    """Workspace lifecycle changes that one owning service can make."""

    DISABLE = "disable"
    RESTORE = "restore"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    """One retained service identity and its current lifecycle state."""

    service_id: str
    display_name: str
    parent_service_id: str | None
    state: LifecycleState
    revision: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class ServiceAdministrationRecord:
    """One complete safe global-administration service record."""

    service_id: str
    display_name: str
    parent_service_id: str | None
    state: LifecycleState
    revision: str
    bootstrap_state: str
    credential_generation: int | None
    prior_generation_expires_at: datetime | None
    bootstrap_audiences: tuple[str, ...] | None
    bootstrap_operations: tuple[str, ...] | None
    bootstrap_workspace_limit: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """One retained workspace identity and its current lifecycle state."""

    workspace_id: str
    caller_reference: str
    display_name: str
    state: LifecycleState
    state_revision: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class LifecycleResult[T]:
    """One stable lifecycle result with replay and change information."""

    value: T
    replayed: bool
    changed: bool

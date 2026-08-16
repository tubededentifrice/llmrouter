"""Coordinate protected MVP administration through existing authorities."""
# ruff: noqa: D102, D107, EM101, PLR0913, PLR0917, PLR2004, TRY003, TRY004

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from llmrouter_backend.accounting import AccountingSummary, PriceComponent
from llmrouter_backend.adapters.openrouter import (
    OPENROUTER_ADAPTER_TYPE,
    OPENROUTER_ENDPOINT,
    OPENROUTER_INSTANCE_SCHEMA,
    OPENROUTER_PROFILE,
    OPENROUTER_ROUTE_SCHEMA,
    OPENROUTER_SUPPORTED_CAPABILITIES,
)
from llmrouter_backend.authority import (
    AuthorityPath,
    OperationPolicy,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.configuration import (
    Assignment,
    AssignmentCandidate,
    ConfigurationScope,
    ConfigurationWriteResult,
    EffectiveConfiguration,
    EffectiveItem,
    PriceAuthority,
    ProviderInstance,
    ProviderModelRoute,
    RegisteredDocument,
    RevisionLayer,
    ScopeConfiguration,
)
from llmrouter_backend.credential_store import (
    CredentialAction,
    CredentialMetadata,
    CredentialOwner,
    CredentialResult,
    SecretInput,
)
from llmrouter_backend.execution import ExecutionKind, ExecutionTarget
from llmrouter_backend.lifecycle import ServiceRecord, WorkspaceRecord

from .model import (
    AssignmentInput,
    CredentialChangeInput,
    CredentialCreateInput,
    ProviderInstanceInput,
    ProviderModelRouteInput,
    RegisteredDocumentInput,
    ServiceStateDocument,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class SessionAuthority(Protocol):
    """Authorize one administrator session."""

    def authorize_session(
        self,
        session_token: str,
        *,
        request_id: str,
        now: datetime,
        policy: OperationPolicy,
        scope: Scope,
        csrf_token: str | None = None,
        origin: str | None = None,
    ) -> RequestContext: ...


class ConfigurationStore(Protocol):
    """Read and publish immutable configuration."""

    def owned(
        self, context: RequestContext, scope: ConfigurationScope
    ) -> RevisionLayer | None: ...

    def effective(
        self, context: RequestContext, scope: ConfigurationScope
    ) -> EffectiveConfiguration: ...

    def publish(
        self,
        context: RequestContext,
        scope: ConfigurationScope,
        content: ScopeConfiguration,
        *,
        expected_active_revision: str | None,
        reason: str,
        now: datetime,
        resource_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigurationWriteResult: ...


class CredentialStore(Protocol):
    """Store write-only credentials and return safe metadata."""

    def create(
        self,
        context: RequestContext,
        *,
        idempotency_key: str,
        owner: CredentialOwner,
        provider_catalog_id: str,
        secret: SecretInput,
        now: datetime,
        safe_label: str | None = None,
    ) -> CredentialResult: ...

    def list_metadata(
        self, context: RequestContext, *, owner: CredentialOwner | None = None
    ) -> tuple[CredentialMetadata, ...]: ...

    def change(
        self,
        context: RequestContext,
        credential_id: str,
        action: CredentialAction,
        *,
        expected_revision: str,
        reason: str,
        now: datetime,
        replacement_secret: SecretInput | None = None,
    ) -> CredentialMetadata: ...


class LifecycleStore(Protocol):
    """Read exact administration lifecycle state."""

    def get_administration_state(
        self,
        context: RequestContext,
        service_id: str,
        *,
        workspace_id: str | None = None,
    ) -> ServiceRecord | WorkspaceRecord: ...


class RequestStatusStore(Protocol):
    """Read content-free request status."""

    def list_status(
        self,
        context: RequestContext,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[tuple[dict[str, object], ...], str | None]: ...

    def status(
        self, context: RequestContext, target: ExecutionTarget
    ) -> dict[str, object]: ...


class AccountingStore(Protocol):
    """Read bounded accounting aggregates."""

    def summary(
        self,
        context: RequestContext,
        scope: Scope,
        *,
        start: datetime,
        end: datetime,
    ) -> AccountingSummary: ...


class AdministrationService:
    """Provide the small protected administration workflow for the MVP."""

    def __init__(
        self,
        *,
        authority: SessionAuthority,
        configuration: ConfigurationStore,
        credentials: CredentialStore,
        lifecycle: LifecycleStore,
        requests: RequestStatusStore,
        accounting: AccountingStore,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._authority = authority
        self._configuration = configuration
        self._credentials = credentials
        self._lifecycle = lifecycle
        self._requests = requests
        self._accounting = accounting
        self._now = now
        self._identity_factory = identity_factory

    def state(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        workspace_id: str | None = None,
    ) -> dict[str, object]:
        """Return one safe exact service or workspace state."""
        scope = Scope(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="health.read",
            scope=scope,
        )
        value = self._lifecycle.get_administration_state(
            context, service_id, workspace_id=workspace_id
        )
        if isinstance(value, WorkspaceRecord):
            return ServiceStateDocument(
                kind="workspace",
                service_id=service_id,
                workspace_id=value.workspace_id,
                display_name=value.display_name,
                state=value.state.value,
                revision=value.state_revision,
            ).model_dump(mode="json")
        return ServiceStateDocument(
            kind="service",
            service_id=value.service_id,
            display_name=value.display_name,
            state=value.state.value,
            revision=value.revision,
            parent_service_id=value.parent_service_id,
        ).model_dump(mode="json")

    def create_credential(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        value: CredentialCreateInput,
        *,
        request_id: str,
    ) -> tuple[dict[str, object], bool]:
        """Encrypt one write-only provider credential."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="credential.manage",
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        owner = CredentialOwner(
            None if value.owner_scope == "global" else _uuid_text(value.owner_scope)
        )
        result = self._credentials.create(
            context,
            idempotency_key=idempotency_key,
            owner=owner,
            provider_catalog_id=value.provider_catalog_id,
            secret=SecretInput(value.secret),
            now=self._now(),
            safe_label=value.safe_label,
        )
        return _credential_document(result.metadata), result.replayed

    def list_credentials(
        self,
        session_token: str,
        *,
        request_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List bounded safe credential metadata."""
        _page_limit(limit)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="credential.manage",
            scope=Scope(),
        )
        values = self._credentials.list_metadata(context)
        page, next_cursor = _page(
            values,
            cursor=cursor,
            limit=limit,
            identity=lambda item: item.credential_id,
        )
        return {
            "items": [_credential_document(item) for item in page],
            "next_cursor": next_cursor,
        }

    def change_credential(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        credential_id: str,
        action: CredentialAction,
        value: CredentialChangeInput,
        *,
        request_id: str,
    ) -> dict[str, object]:
        """Replace, disable, or retire one credential without reading it."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="credential.manage",
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        replacement = (
            None
            if value.replacement_secret is None
            else SecretInput(value.replacement_secret)
        )
        result = self._credentials.change(
            context,
            credential_id,
            action,
            expected_revision=value.expected_revision,
            reason=value.reason,
            now=self._now(),
            replacement_secret=replacement,
        )
        return _credential_document(result)

    def list_provider_instances(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List bounded effective provider instances."""
        effective = self._effective(
            session_token,
            service_id,
            request_id=request_id,
            operation="provider_instance.manage",
        )
        items = tuple(effective.provider_instances)
        page, next_cursor = _page(
            items,
            cursor=cursor,
            limit=limit,
            identity=lambda item: item.stable_id,
        )
        return {
            "items": [_effective_provider_instance(item) for item in page],
            "next_cursor": next_cursor,
        }

    def put_provider_instance(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        value: ProviderInstanceInput,
        *,
        request_id: str,
        provider_instance_id: str | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Create or replace one service-owned OpenRouter instance."""
        supported_operations = value.settings.document.get("supported_operations")
        if (
            value.provider_catalog_id != OPENROUTER_ADAPTER_TYPE
            or value.endpoint != OPENROUTER_ENDPOINT
            or value.settings.schema_name != OPENROUTER_INSTANCE_SCHEMA.schema_name
            or value.settings.major_version != OPENROUTER_INSTANCE_SCHEMA.major_version
            or value.settings.document.get("profile") != OPENROUTER_PROFILE
            or not isinstance(supported_operations, list)
            or not all(isinstance(item, str) for item in supported_operations)
            or set(supported_operations) != OPENROUTER_SUPPORTED_CAPABILITIES
        ):
            raise ValueError("The OpenRouter provider instance is invalid.")
        stable_id = provider_instance_id or str(self._identity_factory())
        _uuid_text(stable_id)
        scope = ConfigurationScope(service_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="provider_instance.manage",
            scope=Scope(service_id),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        layer = self._configuration.owned(context, scope)
        content = ScopeConfiguration() if layer is None else layer.content
        item = ProviderInstance(
            stable_id,
            value.provider_catalog_id,
            value.display_name,
            value.endpoint,
            value.credential_id,
            _registered(value.settings),
            value.state,
            frozenset(value.eligible_service_ids),
        )
        content, created = _replace_provider_instance(content, item)
        result = self._configuration.publish(
            context,
            scope,
            content,
            expected_active_revision=value.expected_revision,
            reason=value.reason,
            now=self._now(),
            resource_id=stable_id,
            idempotency_key=idempotency_key,
        )
        return _write_result(result), created

    def list_provider_model_routes(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List bounded effective provider-model routes."""
        effective = self._effective(
            session_token,
            service_id,
            request_id=request_id,
            operation="provider_route.manage",
        )
        page, next_cursor = _page(
            tuple(effective.provider_model_routes),
            cursor=cursor,
            limit=limit,
            identity=lambda item: item.stable_id,
        )
        return {
            "items": [_effective_route(item) for item in page],
            "next_cursor": next_cursor,
        }

    def put_provider_model_route(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        value: ProviderModelRouteInput,
        *,
        request_id: str,
        provider_model_route_id: str | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Create or replace one service-owned OpenRouter route."""
        if (
            value.settings.schema_name != OPENROUTER_ROUTE_SCHEMA.schema_name
            or value.settings.major_version != OPENROUTER_ROUTE_SCHEMA.major_version
            or not set(value.capabilities) <= OPENROUTER_SUPPORTED_CAPABILITIES
            or len(set(value.capabilities)) != len(value.capabilities)
        ):
            raise ValueError("The OpenRouter model route is invalid.")
        stable_id = provider_model_route_id or str(self._identity_factory())
        _uuid_text(stable_id)
        scope = ConfigurationScope(service_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="provider_route.manage",
            scope=Scope(service_id),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        layer = self._configuration.owned(context, scope)
        content = ScopeConfiguration() if layer is None else layer.content
        route = ProviderModelRoute(
            provider_model_route_id=stable_id,
            provider_instance_id=value.provider_instance_id,
            canonical_model_id=value.canonical_model_id,
            wire_model=value.wire_model,
            capabilities=frozenset(value.capabilities),
            settings=_registered(value.settings),
            price_authority=PriceAuthority(
                value.price_authority.mode,
                value.price_authority.source_name,
                value.price_authority.lookup_identifier,
            ),
            prices=tuple(
                PriceComponent(
                    item.unit,
                    Decimal(item.price),
                    item.currency,
                    item.raw_source_value,
                    Decimal(item.unit_quantity),
                )
                for item in value.prices
            ),
            synchronization_schedule=value.synchronization_schedule,
            stale_after_seconds=value.stale_after_seconds,
            state=value.state,
            eligible_service_ids=frozenset(value.eligible_service_ids),
        )
        content, created = _replace_route(content, route)
        result = self._configuration.publish(
            context,
            scope,
            content,
            expected_active_revision=value.expected_revision,
            reason=value.reason,
            now=self._now(),
            resource_id=stable_id,
            idempotency_key=idempotency_key,
        )
        return _write_result(result), created

    def list_assignments(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        workspace_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List bounded effective assignments."""
        effective = self._effective(
            session_token,
            service_id,
            request_id=request_id,
            operation="assignment.manage",
            workspace_id=workspace_id,
        )
        page, next_cursor = _page(
            tuple(effective.assignments),
            cursor=cursor,
            limit=limit,
            identity=lambda item: item.stable_id,
        )
        return {
            "items": [_effective_assignment(item) for item in page],
            "next_cursor": next_cursor,
        }

    def put_assignment(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        assignment_name: str,
        value: AssignmentInput,
        *,
        request_id: str,
        workspace_id: str | None,
    ) -> dict[str, object]:
        """Publish one complete ordered fallback chain."""
        if not 1 <= len(assignment_name) <= 100:
            raise ValueError("The assignment name is invalid.")
        scope = ConfigurationScope(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="assignment.manage",
            scope=Scope(service_id, workspace_id),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        layer = self._configuration.owned(context, scope)
        content = ScopeConfiguration() if layer is None else layer.content
        assignment = Assignment(
            assignment_name,
            tuple(
                AssignmentCandidate(
                    item.provider_model_route_id, item.attempt_timeout_ms
                )
                for item in value.candidates
            ),
            frozenset(value.required_capabilities),
            value.state,
        )
        content = replace(
            content,
            assignments=_replace_item(
                content.assignments,
                assignment,
                identity=lambda item: item.name,
            )[0],
        )
        result = self._configuration.publish(
            context,
            scope,
            content,
            expected_active_revision=value.expected_revision,
            reason=value.reason,
            now=self._now(),
            resource_id=assignment_name,
            idempotency_key=idempotency_key,
        )
        return _write_result(result)

    def request_status_page(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        workspace_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List content-free request status in one exact scope."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="request_status.read",
            scope=Scope(service_id, workspace_id),
        )
        items, next_cursor = self._requests.list_status(
            context, cursor=cursor, limit=limit
        )
        return {"items": list(items), "next_cursor": next_cursor}

    def request_status(
        self,
        session_token: str,
        service_id: str,
        logical_request_id: str,
        *,
        request_id: str,
        workspace_id: str | None,
    ) -> dict[str, object]:
        """Read one content-free request status in one exact scope."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="request_status.read",
            scope=Scope(service_id, workspace_id),
        )
        return self._requests.status(
            context, ExecutionTarget(ExecutionKind.MODEL, logical_request_id)
        )

    def accounting_summary(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        workspace_id: str | None,
        start: datetime,
        end: datetime,
    ) -> dict[str, object]:
        """Read one bounded exact accounting aggregate."""
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or end - start > timedelta(days=90)
        ):
            raise ValueError("The accounting range is invalid.")
        scope = Scope(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="accounting.read",
            scope=scope,
        )
        result = self._accounting.summary(context, scope, start=start, end=end)
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "currency": result.currency,
            "logical_requests": result.logical_requests,
            "attempts": result.attempts,
            "usage": [
                {"unit": item.unit.value, "quantity": format(item.quantity, "f")}
                for item in result.usage
            ],
            "cost": format(result.cost, "f"),
            "corrections": format(result.corrections, "f"),
        }

    def _effective(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        operation: str,
        workspace_id: str | None = None,
    ) -> EffectiveConfiguration:
        scope = Scope(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation=operation,
            scope=scope,
        )
        return self._configuration.effective(
            context, ConfigurationScope(service_id, workspace_id)
        )

    def _context(
        self,
        session_token: str,
        *,
        request_id: str,
        operation: str,
        scope: Scope,
        mutation: bool = False,
        sensitive: bool = False,
        csrf_token: str | None = None,
        origin: str | None = None,
    ) -> RequestContext:
        policy = OperationPolicy(
            operation,
            AuthorityPath.GLOBAL_ADMINISTRATION,
            frozenset({PrincipalKind.ADMINISTRATOR}),
            scope.kind,
            sensitive=sensitive,
            mutation=mutation,
        )
        return self._authority.authorize_session(
            session_token,
            request_id=request_id,
            now=self._now(),
            policy=policy,
            scope=scope,
            csrf_token=csrf_token,
            origin=origin,
        )


def _uuid_text(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("The identity is invalid.")
    return value


def _registered(value: RegisteredDocumentInput) -> RegisteredDocument:
    schema_name = value.schema_name
    major_version = value.major_version
    document = value.document
    return RegisteredDocument(schema_name, major_version, document)


def _replace_item[T](
    values: tuple[T, ...],
    value: T,
    *,
    identity: Callable[[T], str],
) -> tuple[tuple[T, ...], bool]:
    target = identity(value)
    found = False
    result: list[T] = []
    for item in values:
        if identity(item) == target:
            result.append(value)
            found = True
        else:
            result.append(item)
    if not found:
        result.append(value)
    return tuple(result), not found


def _replace_provider_instance(
    content: ScopeConfiguration, value: ProviderInstance
) -> tuple[ScopeConfiguration, bool]:
    items, created = _replace_item(
        content.provider_instances,
        value,
        identity=lambda item: item.provider_instance_id,
    )
    return replace(content, provider_instances=items), created


def _replace_route(
    content: ScopeConfiguration, value: ProviderModelRoute
) -> tuple[ScopeConfiguration, bool]:
    items, created = _replace_item(
        content.provider_model_routes,
        value,
        identity=lambda item: item.provider_model_route_id,
    )
    return replace(content, provider_model_routes=items), created


def _page_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("The page size must be from 1 through 100.")


def _page[T](
    values: tuple[T, ...],
    *,
    cursor: str | None,
    limit: int,
    identity: Callable[[T], str],
) -> tuple[tuple[T, ...], str | None]:
    _page_limit(limit)
    ordered = tuple(sorted(values, key=identity))
    start = 0
    if cursor is not None:
        positions = [
            index for index, item in enumerate(ordered) if identity(item) == cursor
        ]
        if len(positions) != 1:
            raise ValueError("The page cursor is invalid.")
        start = positions[0] + 1
    page = ordered[start : start + limit]
    next_cursor = (
        identity(page[-1]) if page and start + len(page) < len(ordered) else None
    )
    return page, next_cursor


def _credential_document(value: CredentialMetadata) -> dict[str, object]:
    return {
        "credential_id": value.credential_id,
        "owner_scope": value.owner_scope,
        "provider_catalog_id": value.provider_catalog_id,
        "state": value.state.value,
        "revision": value.revision,
        "created_at": value.created_at.isoformat(),
        "fingerprint": value.fingerprint,
    }


def _write_result(value: ConfigurationWriteResult) -> dict[str, object]:
    return {
        "resource_id": value.resource_id,
        "active_revision": value.active_revision,
        "distribution_state": value.distribution_state.value,
        "operation_id": value.operation_id,
    }


def _registered_document(value: RegisteredDocument) -> dict[str, object]:
    return {
        "schema_name": value.schema_name,
        "major_version": value.major_version,
        "document": dict(value.document),
    }


def _effective_provider_instance(item: EffectiveItem) -> dict[str, object]:
    value = item.value
    if not isinstance(value, ProviderInstance):
        raise RuntimeError("Stored provider instance configuration is invalid.")
    return {
        "provider_instance_id": value.provider_instance_id,
        "owner_scope": item.owner_scope,
        "source_layer": item.source_layer,
        "provider_catalog_id": value.provider_catalog_id,
        "display_name": value.display_name,
        "endpoint": value.endpoint,
        "credential_id": value.credential_id,
        "state": value.state.value,
        "active_revision": item.active_revision,
        "inherited": item.inherited,
        "settings": _registered_document(value.settings),
    }


def _price_document(value: PriceComponent) -> dict[str, str]:
    return {
        "unit": value.unit.value,
        "price": format(value.price, "f"),
        "currency": value.currency,
        "raw_source_value": value.raw_source_value,
        "unit_quantity": format(value.unit_quantity, "f"),
    }


def _effective_route(item: EffectiveItem) -> dict[str, object]:
    value = item.value
    if not isinstance(value, ProviderModelRoute):
        raise RuntimeError("Stored provider route configuration is invalid.")
    return {
        "provider_model_route_id": value.provider_model_route_id,
        "owner_scope": item.owner_scope,
        "source_layer": item.source_layer,
        "provider_instance_id": value.provider_instance_id,
        "canonical_model_id": value.canonical_model_id,
        "wire_model": value.wire_model,
        "capabilities": sorted(value.capabilities),
        "settings": _registered_document(value.settings),
        "price_authority": {
            "mode": value.price_authority.mode.value,
            "source_name": value.price_authority.source_name,
            "lookup_identifier": value.price_authority.lookup_identifier,
        },
        "prices": [_price_document(price) for price in value.prices],
        "synchronization_schedule": value.synchronization_schedule,
        "stale_after_seconds": value.stale_after_seconds,
        "price_version": value.price_version,
        "synchronization_state": (
            value.synchronization_state.value
            if value.synchronization_state is not None
            else None
        ),
        "state": value.state.value,
        "active_revision": item.active_revision,
        "inherited": item.inherited,
    }


def _effective_assignment(item: EffectiveItem) -> dict[str, object]:
    value = item.value
    if not isinstance(value, Assignment):
        raise RuntimeError("Stored assignment configuration is invalid.")
    return {
        "name": value.name,
        "owner_scope": item.owner_scope,
        "source_layer": item.source_layer,
        "state": value.state.value,
        "inherited": item.inherited,
        "active_revision": item.active_revision,
        "candidates": [
            {
                "provider_model_route_id": candidate.provider_model_route_id,
                "attempt_timeout_ms": candidate.attempt_timeout_ms,
            }
            for candidate in value.candidates
        ],
        "required_capabilities": sorted(value.required_capabilities),
    }

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
from llmrouter_backend.budgets import (
    BudgetLimit,
    BudgetScopeKind,
    BudgetTarget,
    EnforcementSummary,
    Money,
    ResetPeriod,
    SignedMoney,
)
from llmrouter_backend.configuration import (
    Assignment,
    AssignmentCandidate,
    CatalogEntry,
    CatalogKind,
    ConfigurationError,
    ConfigurationErrorCode,
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
from llmrouter_backend.lifecycle import (
    LifecycleResult,
    ServiceAction,
    ServiceAdministrationRecord,
    ServiceRecord,
    WorkspaceRecord,
)
from llmrouter_backend.machine_identity import (
    BootstrapCreated,
    BootstrapScope,
    MachineCredentialRepository,
)

from .model import (
    AssignmentInput,
    BudgetLimitInput,
    CredentialChangeInput,
    CredentialCreateInput,
    DiagnosticRunInput,
    ProviderInstanceInput,
    ProviderModelRouteInput,
    RegisteredDocumentInput,
    ServiceActionInput,
    ServiceCreateInput,
    ServiceStateDocument,
    ServiceUpdateInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CONFIGURATION_READ_ATTEMPTS = 3


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

    def create_service_with_bootstrap(
        self,
        context: RequestContext,
        credential_context: RequestContext,
        credentials: MachineCredentialRepository,
        *,
        idempotency_key: str,
        display_name: str,
        parent_service_id: str | None,
        bootstrap_scope: BootstrapScope,
        now: datetime,
    ) -> tuple[LifecycleResult[ServiceRecord], BootstrapCreated | None]: ...

    def list_service_administration(
        self, context: RequestContext
    ) -> tuple[ServiceAdministrationRecord, ...]: ...

    def get_service_administration(
        self, context: RequestContext, service_id: str
    ) -> ServiceAdministrationRecord: ...

    def change_service_metadata(
        self,
        context: RequestContext,
        service_id: str,
        *,
        expected_revision: str,
        display_name: str,
        new_parent_service_id: str | None,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]: ...

    def change_service_state(
        self,
        context: RequestContext,
        service_id: str,
        action: ServiceAction,
        *,
        expected_revision: str,
        idempotency_key: str,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]: ...

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


class BudgetStore(Protocol):
    """Read and change exact hierarchical budgets."""

    def summary(
        self, context: RequestContext, target: BudgetTarget, *, now: datetime
    ) -> EnforcementSummary: ...

    def put_limit(
        self,
        context: RequestContext,
        target: BudgetTarget,
        *,
        hard_limit: Decimal,
        currency: str,
        warning_threshold: Decimal | None,
        reset_period: ResetPeriod,
        expected_revision: str,
        idempotency_key: str,
        now: datetime,
    ) -> BudgetLimit: ...


class DiagnosticRunner(Protocol):
    """Run one authorized exact-route request without returning content."""

    def run(
        self,
        context: RequestContext,
        *,
        logical_request_id: str,
        exact_route_id: str,
        reason: str,
        now: datetime,
    ) -> tuple[dict[str, object], bool]: ...


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
        budgets: BudgetStore | None = None,
        diagnostics: DiagnosticRunner | None = None,
        machine: MachineCredentialRepository | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        identity_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._authority = authority
        self._configuration = configuration
        self._credentials = credentials
        self._lifecycle = lifecycle
        self._requests = requests
        self._accounting = accounting
        self._budgets = budgets
        self._diagnostics = diagnostics
        self._machine = machine
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

    def list_services(
        self,
        session_token: str,
        *,
        request_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List every retained service with safe bootstrap metadata."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="service.manage",
            scope=Scope(),
        )
        page, next_cursor = _page(
            self._lifecycle.list_service_administration(context),
            cursor=cursor,
            limit=limit,
            identity=lambda item: item.service_id,
        )
        return {
            "items": [_service_administration_document(item) for item in page],
            "next_cursor": next_cursor,
        }

    def get_service(
        self, session_token: str, service_id: str, *, request_id: str
    ) -> dict[str, object]:
        """Get one retained service with safe bootstrap metadata."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="service.manage",
            scope=Scope(),
        )
        return _service_administration_document(
            self._lifecycle.get_service_administration(context, service_id)
        )

    def create_service(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        value: ServiceCreateInput,
        *,
        request_id: str,
    ) -> tuple[dict[str, object], bool]:
        """Create one service and bootstrap credential atomically."""
        if self._machine is None:
            raise RuntimeError("The machine credential repository is unavailable.")
        service_context = self._context(
            session_token,
            request_id=request_id,
            operation="service.manage",
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        credential_context = self._context(
            session_token,
            request_id=request_id,
            operation="credential.manage",
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        scope = BootstrapScope(
            frozenset(value.bootstrap_scope.audiences),
            frozenset(value.bootstrap_scope.operations),
            value.bootstrap_scope.workspace_limit,
        )
        lifecycle, bootstrap = self._lifecycle.create_service_with_bootstrap(
            service_context,
            credential_context,
            self._machine,
            idempotency_key=idempotency_key,
            display_name=value.display_name,
            parent_service_id=value.parent_service_id,
            bootstrap_scope=scope,
            now=self._now(),
        )
        return _service_created_document(lifecycle.value, bootstrap), lifecycle.replayed

    def update_service(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        service_id: str,
        value: ServiceUpdateInput,
        *,
        request_id: str,
    ) -> dict[str, object]:
        """Replace one service display name and parent link."""
        read_context = self._context(
            session_token,
            request_id=request_id,
            operation="service.manage",
            scope=Scope(),
        )
        current = self._lifecycle.get_service_administration(read_context, service_id)
        operation = (
            "service_parent.manage"
            if current.parent_service_id != value.new_parent_service_id
            else "service.manage"
        )
        context = self._context(
            session_token,
            request_id=request_id,
            operation=operation,
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        result = self._lifecycle.change_service_metadata(
            context,
            service_id,
            expected_revision=value.expected_revision,
            display_name=value.display_name,
            new_parent_service_id=value.new_parent_service_id,
            reason=value.reason,
        )
        return _service_change_document(result.value)

    def change_service(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        action: ServiceAction,
        value: ServiceActionInput,
        *,
        request_id: str,
    ) -> dict[str, object]:
        """Apply one revision-safe service lifecycle action."""
        context = self._context(
            session_token,
            request_id=request_id,
            operation="service.manage",
            scope=Scope(),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        result = self._lifecycle.change_service_state(
            context,
            service_id,
            action,
            expected_revision=value.expected_revision,
            idempotency_key=idempotency_key,
            reason=value.reason,
        )
        return _service_change_document(result.value)

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

    def list_catalog(
        self,
        session_token: str,
        kind: CatalogKind,
        *,
        request_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List one bounded named global catalog."""
        _page_limit(limit)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="catalog.manage",
            scope=Scope(),
        )
        layer = self._configuration.owned(context, ConfigurationScope())
        revision = None if layer is None else layer.revision_id
        values = (
            ()
            if layer is None
            else tuple(item for item in layer.content.catalog if item.kind is kind)
        )
        item_cursor = None
        if cursor is not None:
            cursor_revision, separator, item_cursor = cursor.partition(":")
            if not separator or not item_cursor:
                raise ConfigurationError(
                    ConfigurationErrorCode.INVALID_REQUEST, request_id
                )
            if revision is None or cursor_revision != revision:
                raise ConfigurationError(
                    ConfigurationErrorCode.REVISION_CONFLICT,
                    request_id,
                    current_revision=revision,
                )
        page, next_cursor = _page(
            values,
            cursor=item_cursor,
            limit=limit,
            identity=lambda item: item.stable_id,
        )
        return {
            "items": (
                []
                if revision is None
                else [_catalog_document(item, revision) for item in page]
            ),
            "next_cursor": (
                None if next_cursor is None else f"{revision}:{next_cursor}"
            ),
            "configuration_revision": revision,
        }

    def budget_summary(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        workspace_id: str | None,
    ) -> dict[str, object]:
        """Read one exact service or workspace budget."""
        store = self._budget_store()
        target = _budget_target(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation="budget.read",
            scope=Scope(service_id, workspace_id),
        )
        return _budget_document(store.summary(context, target, now=self._now()))

    def put_budget(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        value: BudgetLimitInput,
        *,
        request_id: str,
        workspace_id: str | None,
    ) -> dict[str, object]:
        """Replace one exact service or workspace budget."""
        store = self._budget_store()
        target = _budget_target(service_id, workspace_id)
        write_context = self._context(
            session_token,
            request_id=request_id,
            operation="budget.write",
            scope=Scope(service_id, workspace_id),
            mutation=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        result = store.put_limit(
            write_context,
            target,
            hard_limit=Decimal(value.hard_limit),
            currency=value.currency,
            warning_threshold=(
                None
                if value.warning_threshold is None
                else Decimal(value.warning_threshold)
            ),
            reset_period=value.reset_period,
            expected_revision=value.expected_revision,
            idempotency_key=idempotency_key,
            now=self._now(),
        )
        return _budget_limit_document(result)

    def _budget_store(self) -> BudgetStore:
        if self._budgets is None:
            raise RuntimeError("The budget repository is unavailable.")
        return self._budgets

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
        effective, configuration_revision = self._effective_with_owned_revision(
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
            "configuration_revision": configuration_revision,
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
        effective, configuration_revision = self._effective_with_owned_revision(
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
            "configuration_revision": configuration_revision,
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
        effective, configuration_revision = self._effective_with_owned_revision(
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
            "configuration_revision": configuration_revision,
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

    def run_diagnostic(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        idempotency_key: str,
        service_id: str,
        value: DiagnosticRunInput,
        *,
        request_id: str,
        workspace_id: str | None,
    ) -> tuple[dict[str, object], bool]:
        """Run one content-free diagnostic in the exact selected scope."""
        if idempotency_key != value.request_id:
            raise ValueError("The diagnostic idempotency key must match its request.")
        if self._diagnostics is None:
            raise RuntimeError("The administrator diagnostic runner is unavailable.")
        context = self._context(
            session_token,
            request_id=request_id,
            operation="diagnostic.run",
            scope=Scope(service_id, workspace_id),
            mutation=True,
            sensitive=True,
            csrf_token=csrf_token,
            origin=origin,
        )
        return self._diagnostics.run(
            context,
            logical_request_id=value.request_id,
            exact_route_id=value.exact_route,
            reason=value.reason,
            now=self._now(),
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

    def embed_snapshot(
        self,
        contexts: dict[str, RequestContext],
        service_id: str,
        *,
        workspace_id: str | None,
        start: datetime,
        end: datetime,
    ) -> dict[str, object]:
        """Return only the bounded records that an embed grant permits."""
        scope = Scope(service_id, workspace_id)
        for operation, context in contexts.items():
            if (
                context.operation != operation
                or context.authority_path is not AuthorityPath.EMBED
                or context.actor_kind is not PrincipalKind.EMBED
                or context.scope != scope
            ):
                raise ValueError("The embed request context is invalid.")
        result: dict[str, object] = {
            "service_id": service_id,
            "workspace_id": workspace_id,
            "permissions": sorted(contexts),
        }
        health_context = contexts.get("health.read")
        if health_context is not None:
            value = self._lifecycle.get_administration_state(
                health_context, service_id, workspace_id=workspace_id
            )
            if isinstance(value, WorkspaceRecord):
                state = ServiceStateDocument(
                    kind="workspace",
                    service_id=service_id,
                    workspace_id=value.workspace_id,
                    display_name=value.display_name,
                    state=value.state.value,
                    revision=value.state_revision,
                )
            else:
                state = ServiceStateDocument(
                    kind="service",
                    service_id=value.service_id,
                    display_name=value.display_name,
                    state=value.state.value,
                    revision=value.revision,
                    parent_service_id=value.parent_service_id,
                )
            result["state"] = state.model_dump(mode="json")
        configuration_context = contexts.get("configuration.read")
        if configuration_context is not None:
            effective = self._configuration.effective(
                configuration_context, ConfigurationScope(service_id, workspace_id)
            )
            result["configuration"] = {
                "providers": [
                    _embed_provider_instance(item)
                    for item in effective.provider_instances
                ],
                "routes": [
                    _effective_route(item) for item in effective.provider_model_routes
                ],
                "assignments": [
                    _effective_assignment(item) for item in effective.assignments
                ],
            }
        request_context = contexts.get("request_status.read")
        if request_context is not None:
            items, _cursor = self._requests.list_status(request_context, limit=100)
            result["requests"] = list(items)
        accounting_context = contexts.get("accounting.read")
        if accounting_context is not None:
            if (
                start.tzinfo is None
                or end.tzinfo is None
                or end <= start
                or end - start > timedelta(days=7)
            ):
                raise ValueError("The embed accounting range is invalid.")
            summary = self._accounting.summary(
                accounting_context, scope, start=start, end=end
            )
            result["accounting"] = {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "currency": summary.currency,
                "logical_requests": summary.logical_requests,
                "attempts": summary.attempts,
                "usage": [
                    {"unit": item.unit.value, "quantity": format(item.quantity, "f")}
                    for item in summary.usage
                ],
                "cost": format(summary.cost, "f"),
                "corrections": format(summary.corrections, "f"),
            }
        return result

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

    def _effective_with_owned_revision(
        self,
        session_token: str,
        service_id: str,
        *,
        request_id: str,
        operation: str,
        workspace_id: str | None = None,
    ) -> tuple[EffectiveConfiguration, str | None]:
        """Return effective data and the exact target-layer revision."""
        scope = Scope(service_id, workspace_id)
        context = self._context(
            session_token,
            request_id=request_id,
            operation=operation,
            scope=scope,
        )
        configuration_scope = ConfigurationScope(service_id, workspace_id)
        for _attempt in range(_CONFIGURATION_READ_ATTEMPTS):
            owned_before = self._configuration.owned(context, configuration_scope)
            effective = self._configuration.effective(context, configuration_scope)
            owned_after = self._configuration.owned(context, configuration_scope)
            revision_before = None if owned_before is None else owned_before.revision_id
            revision_after = None if owned_after is None else owned_after.revision_id
            if revision_before == revision_after:
                return effective, revision_after
        raise RuntimeError("The configuration changed during the administration read.")

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


def _catalog_document(value: CatalogEntry, revision: str) -> dict[str, object]:
    return {
        "stable_id": value.stable_id,
        "kind": value.kind.value,
        "display_name": value.display_name,
        "capabilities": sorted(value.capabilities),
        "state": value.state.value,
        "settings": (
            None if value.settings is None else _registered_document(value.settings)
        ),
        "active_revision": revision,
    }


def _budget_target(service_id: str, workspace_id: str | None) -> BudgetTarget:
    return BudgetTarget(
        BudgetScopeKind.WORKSPACE
        if workspace_id is not None
        else BudgetScopeKind.SERVICE,
        service_id=service_id,
        workspace_id=workspace_id,
    )


def _money_document(value: Money | SignedMoney) -> dict[str, str]:
    return {"amount": format(value.amount, "f"), "currency": value.currency}


def _budget_document(value: EnforcementSummary) -> dict[str, object]:
    return {
        "scope": value.scope_kind.value,
        "limit": _money_document(value.limit),
        "warning_threshold": (
            None
            if value.warning_threshold is None
            else _money_document(value.warning_threshold)
        ),
        "reserved": _money_document(value.reserved),
        "used": _money_document(value.used),
        "corrected": _money_document(value.corrected),
        "remaining": _money_document(value.remaining),
        "enforcement_state": value.state.value,
        "reset_period": value.reset_period.value,
        "revision": value.revision,
    }


def _budget_limit_document(value: BudgetLimit) -> dict[str, object]:
    return {
        "scope": value.target.kind.value,
        "limit": _money_document(value.hard_limit),
        "warning_threshold": (
            None
            if value.warning_threshold is None
            else _money_document(value.warning_threshold)
        ),
        "reset_period": value.reset_period.value,
        "revision": value.revision,
        "effective_at": value.effective_at.isoformat(),
    }


def _service_administration_document(
    value: ServiceAdministrationRecord,
) -> dict[str, object]:
    result: dict[str, object] = {
        "service_id": value.service_id,
        "display_name": value.display_name,
        "parent_service_id": value.parent_service_id,
        "state": value.state.value,
        "revision": value.revision,
        "bootstrap_state": value.bootstrap_state,
        "credential_generation": value.credential_generation,
        "bootstrap_scope": None,
    }
    if value.prior_generation_expires_at is not None:
        result["prior_generation_expires_at"] = (
            value.prior_generation_expires_at.isoformat()
        )
    if (
        value.bootstrap_audiences is not None
        and value.bootstrap_operations is not None
        and value.bootstrap_workspace_limit is not None
    ):
        result["bootstrap_scope"] = {
            "audiences": list(value.bootstrap_audiences),
            "operations": list(value.bootstrap_operations),
            "workspace_limit": value.bootstrap_workspace_limit,
        }
    return result


def _service_created_document(
    value: ServiceRecord, bootstrap: BootstrapCreated | None
) -> dict[str, object]:
    result: dict[str, object] = {
        "service_id": value.service_id,
        "state": value.state.value,
        "state_revision": value.revision,
        "bootstrap_secret_available": bootstrap is not None,
        "credential_generation": 1,
    }
    if bootstrap is not None:
        result["bootstrap_secret"] = bootstrap.secret.value
    return result


def _service_change_document(value: ServiceRecord) -> dict[str, object]:
    return {
        "resource_id": value.service_id,
        "state": value.state.value,
        "revision": value.revision,
        "operation_id": value.operation_id,
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
        "eligible_service_ids": sorted(value.eligible_service_ids),
        "state": value.state.value,
        "active_revision": item.active_revision,
        "inherited": item.inherited,
        "settings": _registered_document(value.settings),
    }


def _embed_provider_instance(item: EffectiveItem) -> dict[str, object]:
    """Return provider state without a credential reference."""
    document = _effective_provider_instance(item)
    document.pop("credential_id", None)
    return document


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
    price_authority: dict[str, object] = {"mode": value.price_authority.mode.value}
    if value.price_authority.source_name is not None:
        price_authority["source_name"] = value.price_authority.source_name
        price_authority["lookup_identifier"] = value.price_authority.lookup_identifier
    document: dict[str, object] = {
        "provider_model_route_id": value.provider_model_route_id,
        "owner_scope": item.owner_scope,
        "source_layer": item.source_layer,
        "provider_instance_id": value.provider_instance_id,
        "canonical_model_id": value.canonical_model_id,
        "wire_model": value.wire_model,
        "capabilities": sorted(value.capabilities),
        "eligible_service_ids": sorted(value.eligible_service_ids),
        "settings": _registered_document(value.settings),
        "price_authority": price_authority,
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
    if value.embedding_model_space_id is not None:
        document["embedding_model_space_id"] = value.embedding_model_space_id
        document["embedding_dimensions"] = value.embedding_dimensions
    return document


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

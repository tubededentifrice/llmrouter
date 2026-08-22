"""Protected basic administration API tests."""
# ruff: noqa: D101, D102, D103, D107, EM101, FBT003, PLR0913, PLR2004, PT018, S105, TRY003

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from llmrouter_backend.accounting import AccountingSummary
from llmrouter_backend.administration import AdministrationService
from llmrouter_backend.administration.http import (
    install_administration_service,
    router,
)
from llmrouter_backend.administration.model import CredentialCreateInput
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.configuration import (
    ConfigurationScope,
    ConfigurationWriteResult,
    DistributionState,
    EffectiveConfiguration,
    EffectiveItem,
    RevisionLayer,
    ScopeConfiguration,
)
from llmrouter_backend.credential_store import (
    CredentialMetadata,
    CredentialResult,
    CredentialState,
)
from llmrouter_backend.lifecycle import (
    LifecycleResult,
    LifecycleState,
    ServiceAction,
    ServiceAdministrationRecord,
    ServiceRecord,
)
from llmrouter_backend.machine_identity import (
    BootstrapCreated,
    BootstrapScope,
    MachineCredentialRepository,
    SecretValue,
)

if TYPE_CHECKING:
    from llmrouter_backend.authority import OperationPolicy
    from llmrouter_backend.credential_store import (
        CredentialAction,
        CredentialOwner,
        SecretInput,
    )
    from llmrouter_backend.execution import ExecutionTarget

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
SERVICE_ID = "0198a080-0000-7000-8000-000000000001"
SESSION = "s" * 43
CSRF = "c" * 43
ORIGIN = "https://admin.example.test"
SECRET = "test-secret-value"


class FakeAuthority:
    def __init__(self) -> None:
        self.policies: list[OperationPolicy] = []

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
    ) -> RequestContext:
        assert session_token == SESSION
        if policy.mutation and (csrf_token != CSRF or origin != ORIGIN):
            raise PermissionError("browser proof denied")
        self.policies.append(policy)
        return RequestContext(
            request_id=request_id,
            actor_kind=PrincipalKind.ADMINISTRATOR,
            actor_id="administrator-1",
            authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
            authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
            machine_audience=None,
            operation=policy.operation,
            scope=scope,
            authorized_at=now,
            recent_authentication_at=now,
            mutation=policy.mutation,
        )


class FakeConfiguration:
    def __init__(self) -> None:
        self.layer: RevisionLayer | None = None
        self.revision = 49

    def owned(
        self, _context: RequestContext, _scope: ConfigurationScope
    ) -> RevisionLayer | None:
        return self.layer

    def effective(
        self, _context: RequestContext, _scope: ConfigurationScope
    ) -> EffectiveConfiguration:
        if self.layer is not None:
            revision = self.layer.revision_id
            source = self.layer.scope.source_layer

            def item(identity: str, value: object) -> EffectiveItem:
                return EffectiveItem(
                    identity,
                    SERVICE_ID,
                    source,
                    value.state,  # type: ignore[attr-defined]
                    False,
                    revision,
                    value,
                )

            content = self.layer.content
            providers = tuple(
                item(value.provider_instance_id, value)
                for value in content.provider_instances
            )
            routes = tuple(
                item(value.provider_model_route_id, value)
                for value in content.provider_model_routes
            )
            assignments = tuple(
                item(value.name, value) for value in content.assignments
            )
        else:
            revision = str(uuid.UUID(int=9))
            providers = ()
            routes = ()
            assignments = ()
        return EffectiveConfiguration(
            SERVICE_ID,
            None,
            revision,
            DistributionState.CURRENT,
            (),
            providers,
            routes,
            assignments,
        )

    def publish(
        self,
        _context: RequestContext,
        _scope: ConfigurationScope,
        content: ScopeConfiguration,
        *,
        expected_active_revision: str | None,
        reason: str,
        now: datetime,
        resource_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigurationWriteResult:
        assert reason and now == NOW and idempotency_key
        assert content.provider_instances
        assert expected_active_revision == (
            None if self.layer is None else self.layer.revision_id
        )
        self.revision += 1
        revision = str(uuid.UUID(int=self.revision))
        self.layer = RevisionLayer(_scope, revision, content)
        return ConfigurationWriteResult(
            resource_id or "resource",
            revision,
            DistributionState.DISTRIBUTING,
            str(uuid.UUID(int=self.revision + 100)),
        )


class ChangingConfiguration(FakeConfiguration):
    def __init__(self) -> None:
        super().__init__()
        self.effective_reads = 0

    def effective(
        self, context: RequestContext, scope: ConfigurationScope
    ) -> EffectiveConfiguration:
        effective = super().effective(context, scope)
        self.effective_reads += 1
        if self.effective_reads == 1:
            assert self.layer is not None
            self.layer = RevisionLayer(
                self.layer.scope,
                str(uuid.UUID(int=52)),
                self.layer.content,
            )
        return effective


class FakeCredentials:
    def __init__(self) -> None:
        self.received_secret: SecretInput | None = None
        self.values: list[CredentialMetadata] = []

    def create(
        self,
        _context: RequestContext,
        *,
        idempotency_key: str,
        owner: CredentialOwner,
        provider_catalog_id: str,
        secret: SecretInput,
        now: datetime,
        safe_label: str | None = None,
    ) -> CredentialResult:
        assert idempotency_key and owner.public_scope == "global"
        assert safe_label is None
        self.received_secret = secret
        value = CredentialMetadata(
            str(uuid.UUID(int=10)),
            "global",
            provider_catalog_id,
            CredentialState.ACTIVE,
            str(uuid.UUID(int=11)),
            now,
            "safe-fingerprint",
        )
        self.values.append(value)
        return CredentialResult(value, False)

    def list_metadata(
        self, _context: RequestContext, *, owner: CredentialOwner | None = None
    ) -> tuple[CredentialMetadata, ...]:
        assert owner is None
        return tuple(self.values)

    def change(
        self,
        _context: RequestContext,
        credential_id: str,
        _action: CredentialAction,
        *,
        expected_revision: str,
        reason: str,
        now: datetime,
        replacement_secret: SecretInput | None = None,
    ) -> CredentialMetadata:
        raise NotImplementedError


class FakeLifecycle:
    def __init__(self) -> None:
        self.services: list[ServiceAdministrationRecord] = []

    def list_service_administration(
        self, context: RequestContext
    ) -> tuple[ServiceAdministrationRecord, ...]:
        assert context.operation == "service.manage" and context.scope == Scope()
        return tuple(self.services)

    def get_service_administration(
        self, context: RequestContext, service_id: str
    ) -> ServiceAdministrationRecord:
        assert context.operation == "service.manage"
        return next(value for value in self.services if value.service_id == service_id)

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
    ) -> tuple[LifecycleResult[ServiceRecord], BootstrapCreated | None]:
        assert context.operation == "service.manage"
        assert credential_context.operation == "credential.manage"
        assert credentials and idempotency_key and bootstrap_scope and now == NOW
        record = ServiceRecord(
            SERVICE_ID,
            display_name,
            parent_service_id,
            LifecycleState.ACTIVE,
            "1",
            str(uuid.UUID(int=22)),
        )
        return (
            LifecycleResult(record, replayed=False, changed=True),
            BootstrapCreated(SERVICE_ID, 1, SecretValue("b" * 43)),
        )

    def change_service_metadata(
        self,
        context: RequestContext,
        service_id: str,
        *,
        expected_revision: str,
        display_name: str,
        new_parent_service_id: str | None,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]:
        assert context.operation in {"service.manage", "service_parent.manage"}
        assert expected_revision and reason
        return LifecycleResult(
            ServiceRecord(
                service_id,
                display_name,
                new_parent_service_id,
                LifecycleState.ACTIVE,
                "2",
                str(uuid.UUID(int=23)),
            ),
            replayed=False,
            changed=True,
        )

    def change_service_state(
        self,
        context: RequestContext,
        service_id: str,
        action: ServiceAction,
        *,
        expected_revision: str,
        idempotency_key: str,
        reason: str,
    ) -> LifecycleResult[ServiceRecord]:
        assert context.operation == "service.manage"
        assert action is ServiceAction.DISABLE
        assert expected_revision and idempotency_key and reason
        return LifecycleResult(
            ServiceRecord(
                service_id,
                "Service A",
                None,
                LifecycleState.DISABLED,
                "2",
                str(uuid.UUID(int=24)),
            ),
            replayed=False,
            changed=True,
        )

    def get_administration_state(
        self,
        context: RequestContext,
        service_id: str,
        *,
        workspace_id: str | None = None,
    ) -> ServiceRecord:
        assert context.scope.service_id == service_id == SERVICE_ID
        assert workspace_id is None
        return ServiceRecord(
            service_id,
            "Service A",
            None,
            LifecycleState.ACTIVE,
            str(uuid.UUID(int=20)),
            str(uuid.UUID(int=21)),
        )


class FakeRequests:
    def list_status(
        self,
        _context: RequestContext,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[tuple[dict[str, object], ...], str | None]:
        assert cursor is None and limit == 100
        return (({"request_id": str(uuid.UUID(int=30)), "state": "succeeded"},), None)

    def status(
        self, _context: RequestContext, target: ExecutionTarget
    ) -> dict[str, object]:
        return {"request_id": target.public_id, "state": "succeeded"}


class FakeAccounting:
    def summary(
        self,
        _context: RequestContext,
        _scope: Scope,
        *,
        start: datetime,
        end: datetime,
    ) -> AccountingSummary:
        assert start < end
        return AccountingSummary("USD", 1, 1, (), Decimal("0.01"), Decimal(0))


class FakeMachine:
    pass


@pytest.fixture
def lifecycle() -> FakeLifecycle:
    return FakeLifecycle()


@pytest.fixture
def administration(
    lifecycle: FakeLifecycle,
) -> tuple[TestClient, FakeAuthority, FakeCredentials]:
    authority = FakeAuthority()
    credentials = FakeCredentials()
    service = AdministrationService(
        authority=authority,
        configuration=FakeConfiguration(),
        credentials=credentials,
        lifecycle=lifecycle,
        requests=FakeRequests(),
        accounting=FakeAccounting(),
        now=lambda: NOW,
        identity_factory=lambda: uuid.UUID(int=40),
        machine=FakeMachine(),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    install_administration_service(app, service)
    return TestClient(app), authority, credentials


def test_embed_snapshot_rejects_non_embed_context() -> None:
    service = AdministrationService(
        authority=FakeAuthority(),
        configuration=FakeConfiguration(),
        credentials=FakeCredentials(),
        lifecycle=FakeLifecycle(),
        requests=FakeRequests(),
        accounting=FakeAccounting(),
        now=lambda: NOW,
        identity_factory=lambda: uuid.UUID(int=40),
    )
    scope = Scope(SERVICE_ID)
    context = RequestContext(
        request_id="request-1",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="administrator-1",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="health.read",
        scope=scope,
        authorized_at=NOW,
        recent_authentication_at=NOW,
        mutation=False,
    )

    with pytest.raises(ValueError, match="embed request context"):
        service.embed_snapshot(
            {"health.read": context},
            SERVICE_ID,
            workspace_id=None,
            start=NOW,
            end=NOW,
        )


def _headers(*, mutation: bool = False) -> dict[str, str]:
    values = {"cookie": f"__Host-llmrouter-admin={SESSION}"}
    if mutation:
        values.update(
            {
                "content-type": "application/json",
                "x-csrf-token": CSRF,
                "origin": ORIGIN,
                "idempotency-key": "idempotency-key-0001",
            }
        )
    return values


def _provider_instance_body(
    *,
    state: str = "active",
    expected_revision: str | None = None,
    eligible_service_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "provider_catalog_id": "openai_compatible.v1",
        "display_name": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "credential_id": str(uuid.UUID(int=10)),
        "state": state,
        "settings": {
            "schema_name": "adapter.openai_compatible.settings",
            "major_version": 1,
            "document": {
                "profile": "openrouter",
                "supported_operations": ["chat.complete", "chat.stream"],
            },
        },
        "expected_revision": expected_revision,
        "reason": "Change the OpenRouter instance",
        "eligible_service_ids": eligible_service_ids or [],
    }


def _provider_model_route_body(
    *,
    expected_revision: str | None = None,
    eligible_service_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "provider_instance_id": str(uuid.UUID(int=40)),
        "canonical_model_id": str(uuid.UUID(int=41)),
        "wire_model": "deepseek/deepseek-v4-flash",
        "capabilities": ["chat.complete", "chat.stream"],
        "settings": {
            "schema_name": "adapter.openai_compatible.route",
            "major_version": 1,
            "document": {},
        },
        "price_authority": {
            "mode": "manual",
            "source_name": None,
            "lookup_identifier": None,
        },
        "prices": [
            {
                "unit": "input_token",
                "price": "0.1",
                "currency": "USD",
                "raw_source_value": "0.1",
                "unit_quantity": "1",
            }
        ],
        "synchronization_schedule": "0 0 * * 0",
        "stale_after_seconds": 1_209_600,
        "state": "active",
        "expected_revision": expected_revision,
        "reason": "Change the OpenRouter model route",
        "eligible_service_ids": eligible_service_ids or [],
    }


def test_credential_input_is_write_only_and_mutation_is_sensitive(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, authority, credentials = administration
    value = CredentialCreateInput(
        owner_scope="global", provider_catalog_id="openrouter", secret=SECRET
    )
    assert SECRET not in repr(value)

    response = client.post(
        "/v1/admin/credentials",
        headers=_headers(mutation=True),
        json=value.model_dump(mode="json"),
    )

    assert response.status_code == 201
    assert SECRET not in response.text
    assert credentials.received_secret is not None
    assert SECRET not in repr(credentials.received_secret)
    assert authority.policies[-1].sensitive is True
    assert authority.policies[-1].mutation is True


def test_origin_csrf_duplicate_cookie_and_safe_error_are_closed(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    body = {
        "owner_scope": "global",
        "provider_catalog_id": "openrouter",
        "secret": SECRET,
    }
    denied = client.post(
        "/v1/admin/credentials",
        headers={**_headers(mutation=True), "origin": "https://wrong.example"},
        json=body,
    )
    duplicated = client.get(
        "/v1/admin/credentials",
        headers={
            "cookie": (
                f"__Host-llmrouter-admin={SESSION}; __Host-llmrouter-admin={SESSION}"
            )
        },
    )
    assert denied.status_code == 500
    assert SECRET not in denied.text
    assert denied.json()["error"]["code"] == "internal_error"
    assert duplicated.status_code == 401


def test_service_state_and_content_free_status_use_exact_scopes(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, authority, _credentials = administration
    state = client.get(f"/v1/admin/services/{SERVICE_ID}/state", headers=_headers())
    statuses = client.get(
        f"/v1/admin/services/{SERVICE_ID}/model-requests",
        headers=_headers(),
    )
    assert state.status_code == 200
    assert state.json()["service_id"] == SERVICE_ID
    assert statuses.status_code == 200
    assert "result" not in statuses.text
    assert authority.policies[-1].operation == "request_status.read"


def test_provider_configuration_write_requires_recent_authentication(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, authority, _credentials = administration
    response = client.post(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(mutation=True),
        json=_provider_instance_body(),
    )
    assert response.status_code in {200, 201}
    assert authority.policies[-1].operation == "provider_instance.manage"
    assert authority.policies[-1].sensitive is True

    listing = client.get(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(),
    )
    assert listing.status_code == 200
    assert (
        listing.json()["configuration_revision"] == response.json()["active_revision"]
    )
    assert listing.json()["items"][0]["eligible_service_ids"] == []


def test_configuration_listing_retries_a_concurrent_target_revision() -> None:
    configuration = ChangingConfiguration()
    configuration.layer = RevisionLayer(
        ConfigurationScope(SERVICE_ID),
        str(uuid.UUID(int=51)),
        ScopeConfiguration(),
    )
    service = AdministrationService(
        authority=FakeAuthority(),
        configuration=configuration,
        credentials=FakeCredentials(),
        lifecycle=FakeLifecycle(),
        requests=FakeRequests(),
        accounting=FakeAccounting(),
        now=lambda: NOW,
        identity_factory=lambda: uuid.UUID(int=40),
    )

    listing = service.list_provider_instances(
        SESSION,
        SERVICE_ID,
        request_id="request-configuration-consistency",
        cursor=None,
        limit=100,
    )

    assert configuration.effective_reads == 2
    assert listing["items"] == []
    assert listing["configuration_revision"] == str(uuid.UUID(int=52))


def test_provider_listing_returns_safe_eligibility_metadata(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    eligible = [str(uuid.UUID(int=70)), str(uuid.UUID(int=71))]
    created = client.post(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(mutation=True),
        json=_provider_instance_body(eligible_service_ids=eligible),
    )
    listing = client.get(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(),
    )

    assert created.status_code == 201, created.text
    assert listing.status_code == 200
    assert listing.json()["configuration_revision"] == created.json()["active_revision"]
    assert listing.json()["items"][0]["eligible_service_ids"] == eligible


def test_provider_route_listing_returns_safe_eligibility_metadata(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    eligible = [str(uuid.UUID(int=72)), str(uuid.UUID(int=73))]
    provider = client.post(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(mutation=True),
        json=_provider_instance_body(),
    )
    created = client.post(
        f"/v1/admin/services/{SERVICE_ID}/provider-model-routes",
        headers=_headers(mutation=True),
        json=_provider_model_route_body(
            expected_revision=provider.json()["active_revision"],
            eligible_service_ids=eligible,
        ),
    )
    listing = client.get(
        f"/v1/admin/services/{SERVICE_ID}/provider-model-routes",
        headers=_headers(),
    )

    assert provider.status_code == 201, provider.text
    assert created.status_code == 201, created.text
    assert listing.status_code == 200
    assert listing.json()["configuration_revision"] == created.json()["active_revision"]
    assert listing.json()["items"][0]["eligible_service_ids"] == eligible


def test_provider_instance_disable_and_restore_use_expected_revisions(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    resource_id = str(uuid.UUID(int=40))
    created = client.post(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        headers=_headers(mutation=True),
        json=_provider_instance_body(),
    )
    disabled = client.put(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances/{resource_id}",
        headers=_headers(mutation=True),
        json=_provider_instance_body(
            state="disabled", expected_revision=created.json()["active_revision"]
        ),
    )
    restored = client.put(
        f"/v1/admin/services/{SERVICE_ID}/provider-instances/{resource_id}",
        headers=_headers(mutation=True),
        json=_provider_instance_body(
            expected_revision=disabled.json()["active_revision"]
        ),
    )

    assert created.status_code == 201
    assert disabled.status_code == 200
    assert restored.status_code == 200
    assert (
        len(
            {
                created.json()["active_revision"],
                disabled.json()["active_revision"],
                restored.json()["active_revision"],
            }
        )
        == 3
    )


def test_credential_listing_has_bounded_stable_pagination(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, credentials = administration
    for index in range(3):
        credentials.values.append(
            CredentialMetadata(
                str(uuid.UUID(int=100 + index)),
                "global",
                "openrouter",
                CredentialState.ACTIVE,
                str(uuid.UUID(int=200 + index)),
                NOW,
                f"fingerprint-{index}",
            )
        )
    response = client.get(
        "/v1/admin/credentials?limit=2",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert response.json()["next_cursor"] == str(uuid.UUID(int=101))


def test_service_registry_routes_use_exact_global_contract(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
    lifecycle: FakeLifecycle,
) -> None:
    client, authority, _credentials = administration
    lifecycle.services.append(
        ServiceAdministrationRecord(
            SERVICE_ID,
            "Service A",
            None,
            LifecycleState.ACTIVE,
            "1",
            "missing",
            None,
            None,
            None,
            None,
            None,
        )
    )

    listed = client.get("/v1/admin/services", headers=_headers())
    created = client.post(
        "/v1/admin/services",
        headers=_headers(mutation=True),
        json={
            "display_name": "Service A",
            "parent_service_id": None,
            "bootstrap_scope": {
                "audiences": ["data_plane"],
                "operations": ["model.create"],
            },
        },
    )
    updated = client.put(
        f"/v1/admin/services/{SERVICE_ID}",
        headers=_headers(mutation=True),
        json={
            "expected_revision": "1",
            "reason": "Update service metadata",
            "display_name": "Renamed service",
            "new_parent_service_id": None,
        },
    )
    rename_policy = authority.policies[-1]
    disabled = client.post(
        f"/v1/admin/services/{SERVICE_ID}/disable",
        headers=_headers(mutation=True),
        json={"expected_revision": "1", "reason": "Pause new work"},
    )

    assert listed.status_code == 200
    assert listed.json()["items"] == [
        {
            "service_id": SERVICE_ID,
            "display_name": "Service A",
            "parent_service_id": None,
            "state": "active",
            "revision": "1",
            "bootstrap_state": "missing",
            "credential_generation": None,
            "bootstrap_scope": None,
        }
    ]
    assert created.status_code == 201
    assert created.json()["bootstrap_secret"] == "b" * 43
    assert created.json()["bootstrap_secret_available"] is True
    assert updated.status_code == 200 and updated.json()["revision"] == "2"
    assert rename_policy.operation == "service.manage"
    assert rename_policy.sensitive is True
    assert disabled.status_code == 200 and disabled.json()["state"] == "disabled"
    assert [policy.operation for policy in authority.policies[-6:]] == [
        "service.manage",
        "service.manage",
        "credential.manage",
        "service.manage",
        "service.manage",
        "service.manage",
    ]


def test_service_registry_rejects_unknown_actions_without_route_capture(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    response = client.post(
        f"/v1/admin/services/{SERVICE_ID}/rotate-bootstrap",
        headers=_headers(mutation=True),
        json={"expected_revision": "1", "reason": "Unsupported action"},
    )
    assert response.status_code == 404


def test_service_create_rejects_duplicate_bootstrap_scope_values(
    administration: tuple[TestClient, FakeAuthority, FakeCredentials],
) -> None:
    client, _authority, _credentials = administration
    response = client.post(
        "/v1/admin/services",
        headers=_headers(mutation=True),
        json={
            "display_name": "Service A",
            "parent_service_id": None,
            "bootstrap_scope": {
                "audiences": ["data_plane", "data_plane"],
                "operations": ["model.create"],
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"

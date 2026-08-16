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
)
from llmrouter_backend.configuration import (
    ConfigurationWriteResult,
    DistributionState,
    EffectiveConfiguration,
    RevisionLayer,
)
from llmrouter_backend.credential_store import (
    CredentialMetadata,
    CredentialResult,
    CredentialState,
)
from llmrouter_backend.lifecycle import LifecycleState, ServiceRecord

if TYPE_CHECKING:
    from llmrouter_backend.authority import OperationPolicy, Scope
    from llmrouter_backend.configuration import ConfigurationScope, ScopeConfiguration
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
        return EffectiveConfiguration(
            SERVICE_ID,
            None,
            str(uuid.UUID(int=9)),
            DistributionState.CURRENT,
            (),
            (),
            (),
            (),
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


@pytest.fixture
def administration() -> tuple[TestClient, FakeAuthority, FakeCredentials]:
    authority = FakeAuthority()
    credentials = FakeCredentials()
    service = AdministrationService(
        authority=authority,
        configuration=FakeConfiguration(),
        credentials=credentials,
        lifecycle=FakeLifecycle(),
        requests=FakeRequests(),
        accounting=FakeAccounting(),
        now=lambda: NOW,
        identity_factory=lambda: uuid.UUID(int=40),
    )
    app = FastAPI()
    app.include_router(router)
    install_administration_service(app, service)
    return TestClient(app), authority, credentials


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
    *, state: str = "active", expected_revision: str | None = None
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
    assert listing.json() == {"items": [], "next_cursor": None}


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

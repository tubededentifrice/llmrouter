"""Focused administrator diagnostic adapter tests."""
# ruff: noqa: D101, D102, D103, D107, EM102, TRY003

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from llmrouter_backend.administration.diagnostics import (
    AdministratorDiagnosticRunner,
    TransientDiagnosticAuthenticator,
)
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.model_requests.model import CreateModelRequestResult
from llmrouter_backend.routing import DiagnosticGrant
from llmrouter_backend.routing.model import DiagnosticAuthorization

NOW = datetime(2026, 8, 23, 7, tzinfo=UTC)
SERVICE_ID = "0198a080-0000-7000-8000-000000000001"
WORKSPACE_ID = "0198a080-0000-7000-8000-000000000002"
ROUTE_ID = "0198a080-0000-7000-8000-000000000003"
REVISION_ID = "0198a080-0000-7000-8000-000000000004"
REQUEST_ID = "0198a080-0000-7000-8000-000000000005"


class RejectingFallback:
    def authenticate(self, _token: str, *, request_id: str, now: datetime) -> None:
        raise RuntimeError(f"Unexpected fallback for {request_id} at {now.isoformat()}")


class FakeGrants:
    prior: tuple[DiagnosticAuthorization, datetime, str] | None = None

    def find_diagnostic_authorization(
        self,
        context: RequestContext,
        *,
        logical_request_id: str,
    ) -> tuple[DiagnosticAuthorization, datetime, str] | None:
        assert context.scope == Scope(SERVICE_ID, WORKSPACE_ID)
        assert logical_request_id == REQUEST_ID
        return self.prior

    def create_diagnostic_grant(
        self,
        context: RequestContext,
        *,
        exact_route_id: str,
        reason: str,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> DiagnosticGrant:
        assert context.operation == "diagnostic.run"
        assert exact_route_id == ROUTE_ID
        assert reason == "Verify route"
        assert now == NOW
        assert lifetime == timedelta(minutes=5)
        return DiagnosticGrant(
            "0198a080-0000-7000-8000-000000000006",
            "g" * 43,
            SERVICE_ID,
            WORKSPACE_ID,
            ROUTE_ID,
            REVISION_ID,
            NOW + timedelta(minutes=5),
        )


class FakeModels:
    def __init__(self, authenticator: TransientDiagnosticAuthenticator) -> None:
        self._authenticator = authenticator
        self.body: dict[str, object] | None = None
        self.status_document: dict[str, object] = {}

    def create(
        self,
        token: str,
        request_id: str,
        raw_body: bytes,
        *,
        error_request_id: str,
    ) -> CreateModelRequestResult:
        principal = self._authenticator.authenticate(
            token, request_id=error_request_id, now=NOW
        )
        assert principal.service_id == SERVICE_ID
        assert principal.allowed_workspace_ids == frozenset({WORKSPACE_ID})
        assert request_id == REQUEST_ID
        self.body = json.loads(raw_body)
        return CreateModelRequestResult(
            201, {"status_url": f"/v1/model-requests/{request_id}"}
        )

    def authorize_existing(
        self,
        token: str,
        request_id: str,
        operation: str,
        *,
        error_request_id: str,
    ) -> object:
        principal = self._authenticator.authenticate(
            token, request_id=error_request_id, now=NOW
        )
        assert principal.operations == frozenset({"model.create", "model.read"})
        assert request_id == REQUEST_ID
        assert operation == "model.read"
        return object()

    def status(
        self, _scoped: object, request_id: str, *, error_request_id: str
    ) -> dict[str, object]:
        assert request_id == REQUEST_ID
        assert error_request_id == "administrator-operation"
        return self.status_document


def _context(
    *,
    recent: datetime | None = NOW,
    authority_class: AuthorityClass = AuthorityClass.GLOBAL_ADMINISTRATOR,
) -> RequestContext:
    return RequestContext(
        request_id="administrator-operation",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="administrator-1",
        authority_class=authority_class,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation="diagnostic.run",
        scope=Scope(SERVICE_ID, WORKSPACE_ID),
        authorized_at=NOW,
        recent_authentication_at=recent,
        mutation=True,
    )


def test_runner_consumes_one_use_authority_and_returns_no_content() -> None:
    authenticator = TransientDiagnosticAuthenticator(RejectingFallback())  # type: ignore[arg-type]
    models = FakeModels(authenticator)
    runner = AdministratorDiagnosticRunner(
        FakeGrants(),
        models,  # type: ignore[arg-type]
        authenticator,
    )

    result, replayed = runner.run(
        _context(),
        logical_request_id=REQUEST_ID,
        exact_route_id=ROUTE_ID,
        reason="Verify route",
        now=NOW,
    )

    assert replayed is False
    assert result["request_id"] == REQUEST_ID
    assert result["workspace_id"] == WORKSPACE_ID
    assert result["route_configuration_revision"] == REVISION_ID
    serialized = json.dumps(result)
    assert "Reply only" not in serialized
    assert "gggg" not in serialized
    assert "output" not in serialized
    assert models.body is not None
    assert models.body["workspace_id"] == WORKSPACE_ID
    assert models.body["exact_route"] == ROUTE_ID
    assert models.body["exact_route_grant"] == "g" * 43


def test_transient_authority_accepts_an_authorized_service_administrator() -> None:
    authenticator = TransientDiagnosticAuthenticator(RejectingFallback())  # type: ignore[arg-type]
    token = authenticator.issue(
        _context(authority_class=AuthorityClass.SERVICE),
        now=NOW,
    )

    principal = authenticator.authenticate(
        token, request_id="administrator-operation", now=NOW
    )

    assert principal.service_id == SERVICE_ID
    assert principal.allowed_workspace_ids == frozenset({WORKSPACE_ID})


def test_transient_authority_rejects_old_administrator() -> None:
    authenticator = TransientDiagnosticAuthenticator(RejectingFallback())  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        authenticator.issue(
            _context(recent=NOW - timedelta(minutes=6)),
            now=NOW,
        )


def test_runner_returns_current_content_free_replay_without_a_new_grant() -> None:
    authenticator = TransientDiagnosticAuthenticator(RejectingFallback())  # type: ignore[arg-type]
    grants = FakeGrants()
    grants.prior = (
        DiagnosticAuthorization(
            "0198a080-0000-7000-8000-000000000006",
            SERVICE_ID,
            WORKSPACE_ID,
            ROUTE_ID,
            REVISION_ID,
        ),
        NOW + timedelta(minutes=5),
        "Verify route",
    )
    models = FakeModels(authenticator)
    models.status_document = {
        "state": "succeeded",
        "attempts": [],
        "admission": {"status_url": f"/v1/model-requests/{REQUEST_ID}"},
    }
    runner = AdministratorDiagnosticRunner(grants, models, authenticator)  # type: ignore[arg-type]

    result, replayed = runner.run(
        _context(authority_class=AuthorityClass.SERVICE),
        logical_request_id=REQUEST_ID,
        exact_route_id=ROUTE_ID,
        reason="Verify route",
        now=NOW,
    )

    assert replayed is True
    assert result["state"] == "succeeded"
    assert result["phases"][3] == {"name": "provider", "state": "succeeded"}  # type: ignore[index]
    assert models.body is None

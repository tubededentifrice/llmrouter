"""Tests for the backend role application."""

import importlib
from http import HTTPStatus
from typing import Self

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from llmrouter_backend import app
from llmrouter_backend.administration.http import router as administration_router
from llmrouter_backend.embed_sessions.http import router as embed_session_router


def test_health_route_is_registered() -> None:
    """The scaffold registers its process health route."""
    application_paths = {
        route.path for route in app.routes if isinstance(route, APIRoute)
    }
    administration_paths = {
        route.path
        for route in administration_router.routes
        if isinstance(route, APIRoute)
    }
    embed_paths = {
        route.path
        for route in embed_session_router.routes
        if isinstance(route, APIRoute)
    }
    assert "/health" in application_paths
    assert "/ready" in application_paths
    assert "/v1/admin/credentials" in administration_paths
    assert (
        "/v1/admin/services/{service_id}/assignments/{assignment_name}"
        in administration_paths
    )
    assert "/v1/services/{service_id}/administration/embed-sessions" in embed_paths
    assert "/v1/administration/embed-sessions/{session_id}/bootstrap" in embed_paths


def test_readiness_fails_safely_without_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not report readiness when database authority is absent."""
    monkeypatch.delenv("LLMROUTER_DATABASE_URL", raising=False)
    response = TestClient(app).get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"status": "not_ready"}


def test_runtime_component_state_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not report an incomplete runtime as fully ready."""

    class FakeResult:
        def fetchone(self) -> tuple[str]:
            return ("router.services",)

    class FakeConnection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str) -> FakeResult:
            return FakeResult()

    monkeypatch.setenv("LLMROUTER_DATABASE_URL", "postgresql://local/test")
    application_module = importlib.import_module("llmrouter_backend.app")
    monkeypatch.setattr(
        application_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    response = TestClient(app).get("/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "partial",
        "administration": "unavailable",
        "database": "ready",
        "embed_sessions": "unavailable",
        "model_requests": "unavailable",
    }

"""Tests for the backend role application."""

from http import HTTPStatus

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

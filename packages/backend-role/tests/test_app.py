"""Tests for the placeholder backend role."""

from fastapi.routing import APIRoute
from llmrouter_backend import app
from llmrouter_backend.administration.http import router as administration_router


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
    assert "/health" in application_paths
    assert "/v1/admin/credentials" in administration_paths
    assert (
        "/v1/admin/services/{service_id}/assignments/{assignment_name}"
        in administration_paths
    )

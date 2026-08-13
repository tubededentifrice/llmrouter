"""Tests for the placeholder backend role."""

from fastapi.routing import APIRoute
from llmrouter_backend import app


def test_health_route_is_registered() -> None:
    """The scaffold registers its process health route."""
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/health" in paths

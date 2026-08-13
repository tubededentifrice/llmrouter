"""Tests for the Python client scaffold."""

from llmrouter_client import Client


def test_client_keeps_endpoint() -> None:
    """The scaffold keeps the configured endpoint."""
    assert Client(endpoint="http://127.0.0.1:8080").endpoint.endswith(":8080")

"""Tests for the LLM Router web application."""

from __future__ import annotations

import importlib
from http import HTTPStatus
from typing import TYPE_CHECKING, Self

import psycopg
from fastapi.testclient import TestClient
from llmrouter_backend import create_app

if TYPE_CHECKING:
    import pytest

application_module = importlib.import_module("llmrouter_backend.app")


def test_health_does_not_require_the_database() -> None:
    """Keep process health independent from database readiness."""
    response = TestClient(create_app()).get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


def test_readiness_requires_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail readiness when the database URL is absent."""
    monkeypatch.delenv("LLMROUTER_DATABASE_URL", raising=False)
    response = TestClient(create_app()).get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"status": "not_ready"}


def test_readiness_fails_when_the_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a closed response without database error details."""
    monkeypatch.setattr(
        application_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            psycopg.OperationalError("private database detail")
        ),
    )
    response = TestClient(create_app(database_url="postgresql://test")).get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"status": "not_ready"}
    assert "private" not in response.text


def test_readiness_requires_the_clean_foundation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not accept a reachable database without the migration base."""

    class Result:
        def fetchone(self) -> tuple[bool]:
            return (False,)

    class Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> Result:
            assert "router_schema_migrations" in statement
            return Result()

    monkeypatch.setattr(
        application_module.psycopg, "connect", lambda *_args, **_kwargs: Connection()
    )
    response = TestClient(create_app(database_url="postgresql://test")).get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {"status": "not_ready"}


def test_readiness_accepts_the_clean_foundation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report readiness after migration 0001 is present."""

    class Result:
        def fetchone(self) -> tuple[bool]:
            return (True,)

    class Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str) -> Result:
            return Result()

    monkeypatch.setattr(
        application_module.psycopg, "connect", lambda *_args, **_kwargs: Connection()
    )
    response = TestClient(create_app(database_url="postgresql://test")).get("/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ready"}

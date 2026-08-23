"""Integration tests for the clean migration base."""

from __future__ import annotations

import concurrent.futures
import importlib
from http import HTTPStatus
from typing import TYPE_CHECKING

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.database import applied_versions, migrate, migration_plan

if TYPE_CHECKING:
    from pathlib import Path

migrations_module = importlib.import_module("llmrouter_backend.database.migrations")


def test_plan_has_one_reversible_clean_foundation() -> None:
    """Keep the reset migration chain small and explicit."""
    plan = migration_plan()
    assert [(item.version, item.name) for item in plan] == [(1, "foundation")]
    assert all(item.up_sql and item.down_sql for item in plan)


def test_plan_rejects_an_orphan_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject migration SQL that is not one exact reversible pair."""
    (tmp_path / "0001_foundation.up.sql").write_text(
        "CREATE SCHEMA router;", encoding="utf-8"
    )
    (tmp_path / "0001_foundation.down.sql").write_text(
        "DROP SCHEMA router;", encoding="utf-8"
    )
    (tmp_path / "0002_orphan.down.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(migrations_module, "files", lambda _package: tmp_path)
    with pytest.raises(RuntimeError, match="exact up and down pairs"):
        migration_plan()


def test_foundation_migrates_up_down_and_up(database_url: str) -> None:
    """Apply, remove, and reapply the clean schema base."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        assert applied_versions(connection) == (1,)
        assert connection.execute("SELECT to_regnamespace('router')").fetchone() == (
            "router",
        )

        migrate(connection, target=0)
        assert applied_versions(connection) == ()
        assert connection.execute("SELECT to_regnamespace('router')").fetchone() == (
            None,
        )

        migrate(connection)
        assert applied_versions(connection) == (1,)


def test_migration_rejects_a_stale_pre_reset_history(database_url: str) -> None:
    """Require an explicit clean reset instead of converting old data."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            """UPDATE public.router_schema_migrations
               SET name = 'old_foundation'
               WHERE version = 1"""
        )
        with pytest.raises(RuntimeError, match="does not match the repository"):
            migrate(connection)


def test_readiness_rejects_stale_or_extra_migration_history(database_url: str) -> None:
    """Require the exact current migration plan before service starts."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        client = TestClient(create_app(database_url=database_url))
        assert client.get("/ready").status_code == HTTPStatus.OK

        connection.execute(
            """UPDATE public.router_schema_migrations
               SET checksum = repeat('0', 64)
               WHERE version = 1"""
        )
        assert client.get("/ready").status_code == HTTPStatus.SERVICE_UNAVAILABLE

        foundation = migration_plan()[0]
        connection.execute(
            """UPDATE public.router_schema_migrations
               SET checksum = %s
               WHERE version = 1""",
            (foundation.checksum,),
        )
        connection.execute(
            """INSERT INTO public.router_schema_migrations (version, name, checksum)
               VALUES (2, 'stale_history', repeat('0', 64))"""
        )
        assert client.get("/ready").status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_foundation_down_refuses_unexpected_objects(database_url: str) -> None:
    """Keep unknown schema data and history when a rollback is unsafe."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate(connection)
        connection.execute("CREATE TABLE router.unexpected_data (value integer)")

        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            migrate(connection, target=0)

        assert applied_versions(connection) == (1,)
        assert connection.execute(
            "SELECT to_regclass('router.unexpected_data')"
        ).fetchone() == ("router.unexpected_data",)


def test_concurrent_migration_is_serialized(database_url: str) -> None:
    """Let identical application replicas migrate one database safely."""

    def migrate_once() -> tuple[int, ...]:
        with psycopg.connect(database_url, autocommit=True) as connection:
            migrate(connection)
            return applied_versions(connection)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: migrate_once(), range(2)))
    assert results == ((1,), (1,))

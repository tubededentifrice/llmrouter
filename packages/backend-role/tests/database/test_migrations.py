"""Integration tests for the clean migration base."""

from __future__ import annotations

import concurrent.futures

import psycopg
import pytest
from llmrouter_backend.database import applied_versions, migrate, migration_plan


def test_plan_has_one_reversible_clean_foundation() -> None:
    """Keep the reset migration chain small and explicit."""
    plan = migration_plan()
    assert [(item.version, item.name) for item in plan] == [(1, "foundation")]
    assert all(item.up_sql and item.down_sql for item in plan)


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


def test_concurrent_migration_is_serialized(database_url: str) -> None:
    """Let identical application replicas migrate one database safely."""

    def migrate_once() -> tuple[int, ...]:
        with psycopg.connect(database_url, autocommit=True) as connection:
            migrate(connection)
            return applied_versions(connection)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: migrate_once(), range(2)))
    assert results == ((1,), (1,))

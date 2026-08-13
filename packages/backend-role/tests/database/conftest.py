"""Isolated PostgreSQL databases for migration integration tests."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def database_url() -> Iterator[str]:
    """Create one temporary database and remove it after the test."""
    administrator_url = os.environ.get("LLMROUTER_TEST_DATABASE_URL")
    if administrator_url is None:
        pytest.skip("LLMROUTER_TEST_DATABASE_URL is not set.")

    database_name = f"llmrouter_test_{uuid.uuid4().hex}"
    with psycopg.connect(administrator_url, autocommit=True) as administrator:
        administrator.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    connection_values = conninfo_to_dict(administrator_url)
    connection_values["dbname"] = database_name
    clean_connection_values = {
        key: str(value) for key, value in connection_values.items() if value is not None
    }
    test_url = make_conninfo(**clean_connection_values)
    try:
        yield test_url
    finally:
        with psycopg.connect(administrator_url, autocommit=True) as administrator:
            administrator.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )

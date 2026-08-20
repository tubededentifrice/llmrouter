"""Apply the complete local development database migration plan."""
# ruff: noqa: EM101, INP001, TRY003

from __future__ import annotations

import os

import psycopg
from llmrouter_backend.database import migrate


def main() -> None:
    """Apply migrations without printing connection or secret values."""
    database_url = os.environ.get("LLMROUTER_DATABASE_URL")
    if database_url is None:
        raise SystemExit("LLMROUTER_DATABASE_URL is required.")
    with psycopg.connect(database_url) as connection:
        migrate(connection)
    print("Local database migrations passed.")


if __name__ == "__main__":
    main()

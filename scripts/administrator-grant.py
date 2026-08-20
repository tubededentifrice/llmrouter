"""Create one short-lived, one-use Router administrator grant URL."""
# ruff: noqa: EM101, INP001, TRY003

from __future__ import annotations

import argparse
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llmrouter_backend.admin_auth import TrustedGrantPurpose
from llmrouter_backend.admin_auth.deployment import configured_repository
from llmrouter_backend.authority import ADMINISTRATOR_OPERATIONS


def main() -> None:
    """Print only the show-once URL and its expiry."""
    parser = argparse.ArgumentParser()
    parser.add_argument("purpose", choices=("initial", "recovery"))
    arguments = parser.parse_args()
    database_url = os.environ.get("LLMROUTER_DATABASE_URL")
    if database_url is None and Path("/run/secrets/postgres_password").is_file():
        password = Path("/run/secrets/postgres_password").read_text().strip()
        database_url = f"postgresql://llmrouter:{password}@postgres:5432/llmrouter"
    if database_url is None:
        raise SystemExit("The Router database URL is unavailable.")
    repository = configured_repository(database_url)
    if repository is None:
        raise SystemExit("Public administrator authentication is not configured.")
    now = datetime.now(UTC)
    result = repository.create_trusted_grant_url(
        TrustedGrantPurpose(arguments.purpose),
        ADMINISTRATOR_OPERATIONS,
        request_id=str(uuid.uuid4()),
        now=now,
        expires_at=now + timedelta(minutes=10),
    )
    print(result.url)
    print(f"Expires at: {result.expires_at.isoformat()}")


if __name__ == "__main__":
    main()

"""Ordered and checksum-verified PostgreSQL migrations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from psycopg import Connection

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.up\.sql$")
_MIGRATION_LOCK = 4_993_044_345_821


@dataclass(frozen=True)
class Migration:
    """One reversible database migration."""

    version: int
    name: str
    up_sql: str
    down_sql: str
    checksum: str


def migration_plan() -> tuple[Migration, ...]:
    """Load all migration pairs in contiguous version order."""
    directory = files(__package__)
    result: list[Migration] = []
    for item in directory.iterdir():
        match = _MIGRATION_NAME.fullmatch(item.name)
        if match is None:
            continue
        down_item = directory.joinpath(item.name.replace(".up.sql", ".down.sql"))
        if not down_item.is_file():
            msg = f"Rollback migration is missing for {item.name}."
            raise RuntimeError(msg)
        up_sql = item.read_text(encoding="utf-8")
        down_sql = down_item.read_text(encoding="utf-8")
        checksum_input = b"up\0" + up_sql.encode() + b"\0down\0" + down_sql.encode()
        result.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                up_sql=up_sql,
                down_sql=down_sql,
                checksum=hashlib.sha256(checksum_input).hexdigest(),
            )
        )
    result.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in result]
    if not result or versions != list(range(1, len(result) + 1)):
        msg = "Migration versions must start at 0001 and remain contiguous."
        raise RuntimeError(msg)
    return tuple(result)


def _ensure_history(connection: Connection[Any]) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS public.router_schema_migrations (
               version integer PRIMARY KEY CHECK (version > 0),
               name text NOT NULL UNIQUE,
               checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
               applied_at timestamptz NOT NULL DEFAULT transaction_timestamp()
           )"""
    )


def applied_versions(connection: Connection[Any]) -> tuple[int, ...]:
    """Return applied versions after checksum validation."""
    plan = {migration.version: migration for migration in migration_plan()}
    _ensure_history(connection)
    rows = connection.execute(
        """SELECT version, name, checksum
           FROM public.router_schema_migrations
           ORDER BY version"""
    ).fetchall()
    versions: list[int] = []
    for version, name, checksum in rows:
        migration = plan.get(version)
        if (
            migration is None
            or migration.name != name
            or migration.checksum != checksum
        ):
            msg = f"Applied migration {version:04d} does not match the repository."
            raise RuntimeError(msg)
        versions.append(version)
    if versions != list(range(1, len(versions) + 1)):
        msg = "Applied migration versions must form a contiguous prefix."
        raise RuntimeError(msg)
    return tuple(versions)


def _pending(
    plan: Iterable[Migration], current: set[int], target: int
) -> Iterable[Migration]:
    return (
        migration
        for migration in plan
        if migration.version <= target and migration.version not in current
    )


def migrate(connection: Connection[Any], target: int | None = None) -> None:
    """Move the schema to the selected version in one transaction."""
    plan = migration_plan()
    maximum = plan[-1].version
    selected_target = maximum if target is None else target
    if not 0 <= selected_target <= maximum:
        msg = f"Migration target must be between 0 and {maximum}."
        raise ValueError(msg)

    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        current = set(applied_versions(connection))
        for migration in sorted(
            (
                item
                for item in plan
                if item.version in current and item.version > selected_target
            ),
            key=lambda item: item.version,
            reverse=True,
        ):
            connection.execute(migration.down_sql)
            connection.execute(
                "DELETE FROM public.router_schema_migrations WHERE version = %s",
                (migration.version,),
            )

        current = set(applied_versions(connection))
        for migration in _pending(plan, current, selected_target):
            connection.execute(migration.up_sql)
            connection.execute(
                """INSERT INTO public.router_schema_migrations (
                       version, name, checksum
                   ) VALUES (%s, %s, %s)""",
                (migration.version, migration.name, migration.checksum),
            )

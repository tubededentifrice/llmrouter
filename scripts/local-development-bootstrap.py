"""Create deterministic local identities and a short-lived example token."""
# ruff: noqa: EM101, EM102, INP001, PLR2004, TRY003

from __future__ import annotations

import hashlib
import os
import stat
from base64 import urlsafe_b64decode
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from llmrouter_backend.adapters.openrouter import openrouter_registered_schemas
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    BudgetScopeKind,
    BudgetTarget,
    PostgresBudgetRepository,
    ResetPeriod,
)
from llmrouter_backend.configuration import (
    CatalogEntry,
    CatalogKind,
    ConfigurationScope,
    PostgresConfigurationRepository,
    RegisteredDocument,
    ScopeConfiguration,
    SettingsSchemaRegistry,
)
from llmrouter_backend.configuration.errors import ConfigurationError
from llmrouter_backend.machine_identity import (
    BootstrapScope,
    MachineCredentialRepository,
    WorkspaceLimit,
)

SERVICE_ID = "0198a080-0000-7000-8000-000000000101"
DEEPSEEK_CANONICAL_MODEL_ID = "0198a080-0000-7000-8000-000000000120"
MIMO_CANONICAL_MODEL_ID = "0198a080-0000-7000-8000-000000000121"
GRANITE_CANONICAL_MODEL_ID = "0198a080-0000-7000-8000-000000000122"
WORKSPACE_IDS = (
    "0198a080-0000-7000-8000-000000000102",
    "0198a080-0000-7000-8000-000000000103",
)
STATE_DIRECTORY = Path("/local-state")


def main() -> None:
    """Seed stable scopes and refresh the example host token."""
    database_url = _required_environment("LLMROUTER_DATABASE_URL")
    digest_key = _secret_bytes(Path("/run/secrets/machine_digest_key"))
    current = datetime.now(UTC)
    with psycopg.connect(database_url) as connection:
        _seed_scopes(connection)
        has_generation = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM router.service_bootstrap_generations "
            "WHERE service_id = %s)",
            (SERVICE_ID,),
        ).fetchone() == (True,)
    _seed_catalog(database_url, current)
    _seed_budget(database_url, current)
    machine = MachineCredentialRepository(
        database_url,
        issuer="llmrouter-local-development",
        digest_keys={"local-v1": digest_key},
        current_digest_key_id="local-v1",
    )
    bootstrap_path = STATE_DIRECTORY / "service-bootstrap"
    if not has_generation:
        created = machine.create_initial_bootstrap(
            _administrator_context(current),
            SERVICE_ID,
            BootstrapScope(
                audiences=frozenset({Audience.HOST_BACKEND, Audience.DATA_PLANE}),
                operations=frozenset(
                    {"admin_embed.create", "model.create", "model.read", "model.cancel"}
                ),
                workspace_limit=WorkspaceLimit.EXPLICIT_ONLY,
            ),
            now=current,
        )
        _write_secret(bootstrap_path, created.secret.value)
    elif not bootstrap_path.is_file() or bootstrap_path.is_symlink():
        raise SystemExit(
            "The local bootstrap secret is unavailable. Run the reset command."
        )
    token = machine.exchange(
        request_id="local-example-token",
        service_id=SERVICE_ID,
        bootstrap_secret=_read_secret(bootstrap_path),
        audience=Audience.HOST_BACKEND,
        operations=frozenset({"admin_embed.create"}),
        workspace_ids=frozenset(WORKSPACE_IDS),
        now=current,
    )
    _write_secret(STATE_DIRECTORY / "example-host-token", token.access_token.value)
    data_token = machine.exchange(
        request_id="local-data-plane-token",
        service_id=SERVICE_ID,
        bootstrap_secret=_read_secret(bootstrap_path),
        audience=Audience.DATA_PLANE,
        operations=frozenset({"model.create", "model.read", "model.cancel"}),
        workspace_ids=frozenset(WORKSPACE_IDS),
        now=current,
    )
    _write_secret(STATE_DIRECTORY / "data-plane-token", data_token.access_token.value)
    print("Local development identities are ready.")


def _seed_budget(database_url: str, current: datetime) -> None:
    """Create one bounded local service budget with an idempotent operation."""
    repository = PostgresBudgetRepository(database_url)
    repository.put_limit(
        _administrator_context(current, operation="budget.write", service_scope=True),
        BudgetTarget(BudgetScopeKind.SERVICE, service_id=SERVICE_ID),
        hard_limit=Decimal(5),
        currency="USD",
        warning_threshold=Decimal(4),
        reset_period=ResetPeriod.NONE,
        expected_revision="0",
        idempotency_key="local-service-budget-v1",
        now=current,
    )


def _seed_catalog(database_url: str, current: datetime) -> None:
    """Publish the accepted local OpenRouter and model catalog once."""
    with psycopg.connect(database_url) as connection:
        active = connection.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM router.active_configurations
                   WHERE scope_kind = 'global'
               )"""
        ).fetchone() == (True,)
    if active:
        return
    repository = PostgresConfigurationRepository(
        database_url,
        schema_registry=SettingsSchemaRegistry(openrouter_registered_schemas()),
    )
    content = ScopeConfiguration(
        catalog=(
            CatalogEntry(
                CatalogKind.PROVIDER,
                "openai_compatible.v1",
                "OpenRouter",
                frozenset({"chat.complete", "chat.stream"}),
                settings=RegisteredDocument(
                    "adapter.openai_compatible.settings",
                    1,
                    {
                        "profile": "openrouter",
                        "supported_operations": ["chat.complete", "chat.stream"],
                    },
                ),
            ),
            CatalogEntry(
                CatalogKind.MODEL,
                DEEPSEEK_CANONICAL_MODEL_ID,
                "DeepSeek V4 Flash",
                frozenset({"chat.complete", "chat.stream"}),
            ),
            CatalogEntry(
                CatalogKind.MODEL,
                MIMO_CANONICAL_MODEL_ID,
                "MiMo 2.5",
                frozenset({"chat.complete", "chat.stream"}),
            ),
            CatalogEntry(
                CatalogKind.MODEL,
                GRANITE_CANONICAL_MODEL_ID,
                "Granite 4.1 8B",
                frozenset({"chat.complete", "chat.stream"}),
            ),
        )
    )
    try:
        repository.publish(
            _administrator_context(current, operation="catalog.manage"),
            ConfigurationScope(),
            content,
            expected_active_revision=None,
            reason="Create the deterministic local model catalog",
            now=current,
            resource_id="local-model-catalog",
            idempotency_key="local-model-catalog-v1",
        )
    except ConfigurationError as error:
        details = "; ".join(
            f"{issue.field_path}: {issue.reason}" for issue in error.issues
        )
        raise SystemExit(f"The local catalog is invalid. {details}") from error


def _seed_scopes(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        INSERT INTO router.services (id, stable_name, display_name)
        VALUES (%s, 'local-example-service', 'Local example service')
        ON CONFLICT (id) DO NOTHING
        """,
        (SERVICE_ID,),
    )
    for index, workspace_id in enumerate(WORKSPACE_IDS, start=1):
        connection.execute(
            """
            INSERT INTO router.workspaces (
                id, service_id, caller_reference, creation_idempotency_key,
                creation_fingerprint, display_name
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                workspace_id,
                SERVICE_ID,
                f"local-workspace-{index}",
                f"local-workspace-create-{index}",
                hashlib.sha256(f"local-workspace-{index}".encode()).digest(),
                f"Local workspace {index}",
            ),
        )


def _administrator_context(
    current: datetime,
    *,
    operation: str = "credential.manage",
    service_scope: bool = False,
) -> RequestContext:
    return RequestContext(
        request_id="local-bootstrap-create",
        actor_kind=PrincipalKind.ADMINISTRATOR,
        actor_id="local-development-setup",
        authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
        authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
        machine_audience=None,
        operation=operation,
        scope=Scope(SERVICE_ID) if service_scope else Scope(),
        authorized_at=current,
        recent_authentication_at=current,
        mutation=True,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(f"{name} is required.")
    return value


def _secret_bytes(path: Path) -> bytes:
    try:
        value = urlsafe_b64decode(_read_secret(path) + "=")
    except ValueError as error:
        raise SystemExit("A local deployment key is invalid.") from error
    if len(value) != 32:
        raise SystemExit("A local deployment key is invalid.")
    return value


def _write_secret(path: Path, value: str) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_NOFOLLOW
    directory = os.open(path.parent, directory_flags)
    try:
        try:
            descriptor = os.open(path.name, file_flags, dir_fd=directory)
        except FileNotFoundError:
            descriptor = os.open(
                path.name,
                file_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory,
            )
        with os.fdopen(descriptor, "wb", closefd=True) as secret:
            metadata = os.fstat(secret.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SystemExit("A local secret path is unsafe.")
            os.fchmod(secret.fileno(), 0o600)
            os.ftruncate(secret.fileno(), 0)
            secret.write((value + "\n").encode("ascii"))
            secret.flush()
            os.fsync(secret.fileno())
    except OSError as error:
        raise SystemExit("A local secret path is unsafe.") from error
    finally:
        os.close(directory)


def _read_secret(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb", closefd=True) as secret:
            metadata = os.fstat(secret.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SystemExit("A local secret path is unsafe.")
            return secret.read().decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise SystemExit("A local secret path is unsafe.") from error


if __name__ == "__main__":
    main()
